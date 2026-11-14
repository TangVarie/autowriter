"""F01: failed reads must not produce an overwrite task; stale tasks must not write."""
from __future__ import annotations

import uuid

import pytest

from deskcore import core, store, tools
from fakes import FakeClient

PID = '11111111-1111-1111-1111-111111111111'
UID = '22222222-2222-2222-2222-222222222222'
REV = '2026-11-14T10:00:00+00:00'
E1, E2 = str(uuid.UUID(int=1)), str(uuid.UUID(int=2))


class CalibrationClient(FakeClient):
    def __init__(self, *, fail_read=False):
        super().__init__(rows={
            'projects': [{'id': PID, 'owner_id': UID}],
            'user_calibration_notes': [{'project_id': PID, 'user_id': UID, 'notes': 'old rule', 'updated_at': REV}],
            'style_edits': [{'id': E1, 'project_id': PID, 'user_id': UID, 'distilled': False,
                             'created_at': REV, 'ai_body': 'before', 'my_body': 'after'}],
        })
        self.fail_read = fail_read

    def _execute(self, q):
        if q.table_name == 'user_calibration_notes' and q.op == 'select' and self.fail_read:
            self.fail_read = False
            raise TimeoutError('synthetic read failure')
        if q.table_name == 'style_edits' and q.op == 'insert':
            q.payload.setdefault('distilled', False)
            q.payload.setdefault('created_at', REV)
        return super()._execute(q)


def test_a_failed_old_note_read_does_not_become_empty_history():
    c = CalibrationClient(fail_read=True)
    with pytest.raises(store.CalibrationReadError):
        core.build_distillation_task(c, PID, user_id=UID)
    assert c.rows['user_calibration_notes'][0]['notes'] == 'old rule'
    assert c.rows['style_edits'][0]['distilled'] is False


def test_tool_preserves_a_saved_edit_without_sending_an_overwrite_task(monkeypatch):
    c = CalibrationClient(fail_read=True)
    monkeypatch.setattr(core, 'sb', lambda: c)
    out = tools.record_edit(PID, 'synthetic title', 'before', 'synthetic title', 'changed', _user_id=UID)
    assert out['stored'] is True and out['distillation_status'] == 'pending'
    assert 'distillation_task' not in out
    assert '不要重调 record_edit' in out['next_step'] and 'my_style' in out['next_step']
    assert len(c.rows['style_edits']) == 2 and all(not e['distilled'] for e in c.rows['style_edits'])
    # Connectivity recovered, but an old caller still cannot overwrite notes and mark edits without a revision.
    with pytest.raises(tools.InvalidInput, match='expected_revision'):
        tools.save_my_style(PID, 'new observation only', edit_ids=[E1], _user_id=UID)
    assert c.rows['user_calibration_notes'][0]['notes'] == 'old rule'
    assert all(not e['distilled'] for e in c.rows['style_edits'])
    recovered = core.build_distillation_task(c, PID, user_id=UID)
    assert recovered['existing_notes'] == 'old rule' and recovered['expected_revision'] == REV


def test_missing_row_is_distinct_from_read_failure():
    c = CalibrationClient()
    c.rows['user_calibration_notes'] = []
    task = core.build_distillation_task(c, PID, user_id=UID)
    assert task['existing_notes'] == '' and task['expected_revision'] == 'new'
    assert 'expected_revision' in task['instruction']


def test_an_existing_note_without_a_revision_is_not_a_new_note():
    c = CalibrationClient()
    c.rows['user_calibration_notes'][0]['updated_at'] = None
    with pytest.raises(store.CalibrationReadError):
        core.build_distillation_task(c, PID, user_id=UID)


def test_task_snapshots_only_eight_pending_edits_and_reports_the_remainder():
    c = CalibrationClient()
    c.rows['style_edits'] = [dict(c.rows['style_edits'][0], id=str(uuid.UUID(int=n))) for n in range(1, 13)]
    task = core.build_distillation_task(c, PID, user_id=UID)
    assert task['edit_count'] == len(task['edit_ids']) == 8 and task['more_pending'] == 4
    assert task['existing_notes'] == 'old rule' and task['expected_revision'] == REV


def test_an_unchanged_edit_does_not_create_a_distillation_task():
    c = CalibrationClient()
    out = core.record_edit(c, PID, user_id=UID, ai_title='same', my_title='same', ai_body='same', my_body='same')
    assert out['stored'] is False and 'distillation_task' not in out
    assert len(c.rows['style_edits']) == 1


def test_my_style_recovers_a_task_after_the_original_conversation_ended():
    c = CalibrationClient()
    out = core.my_style(c, PID, user_id=UID)
    assert out['pending_distillation'] == 1
    assert out['pending_distillation_task']['edit_ids'] == [E1]
    assert out['pending_distillation_task']['expected_revision'] == REV
    c.rows['style_edits'][0]['distilled'] = True
    assert 'pending_distillation_task' not in core.my_style(c, PID, user_id=UID)


def test_stale_cas_does_not_fall_back_to_blind_upsert_or_mark_edits():
    c = CalibrationClient()
    task = core.build_distillation_task(c, PID, user_id=UID)
    c.rows['user_calibration_notes'][0].update(notes='concurrent update', updated_at='2026-11-14T10:01:00Z')
    c.rpc_impl['deskcore_save_style_cas'] = lambda args: []
    with pytest.raises(ValueError, match='快照已变化'):
        core.save_my_style(c, PID, 'stale result', user_id=UID,
                           edit_ids=task['edit_ids'], expected_revision=task['expected_revision'])
    assert c.rows['user_calibration_notes'][0]['notes'] == 'concurrent update'
    assert c.rows['style_edits'][0]['distilled'] is False
    assert not any(q['op'] in ('upsert', 'update') for q in c.calls)


def test_missing_rpc_has_no_blind_write_fallback():
    c = CalibrationClient()
    def missing(_args):
        raise RuntimeError('synthetic missing RPC')
    c.rpc_impl['deskcore_save_style_cas'] = missing
    with pytest.raises(RuntimeError, match='missing RPC'):
        core.save_my_style(c, PID, 'new notes', user_id=UID, edit_ids=[E1], expected_revision=REV)
    assert c.rows['user_calibration_notes'][0]['notes'] == 'old rule'
    assert c.rows['style_edits'][0]['distilled'] is False


def test_store_passes_revision_and_exact_edit_ids_in_one_rpc():
    c = CalibrationClient()
    c.rpc_impl['deskcore_save_style_cas'] = lambda args: [{'revision': REV, 'edits_absorbed': 1}]
    out = core.save_my_style(c, PID, 'complete notes', user_id=UID, edit_ids=[E1, E1], expected_revision=REV)
    assert out['saved'] is True and out['edits_absorbed'] == 1 and out['revision'] == REV
    assert c.rpc_calls == [('deskcore_save_style_cas', {
        '_project_id': PID, '_user_id': UID, '_notes': 'complete notes',
        '_expected_updated_at': REV, '_edit_ids': [E1],
    })]
    assert not any(q['op'] in ('upsert', 'update') for q in c.calls)


def test_manual_update_does_not_absorb_edits_and_read_failure_refuses_write():
    c = CalibrationClient(fail_read=True)
    with pytest.raises(store.CalibrationReadError):
        core.save_my_style(c, PID, 'manual notes', user_id=UID)
    assert c.rpc_calls == []
    c.rpc_impl['deskcore_save_style_cas'] = lambda args: [{'revision': REV, 'edits_absorbed': 0}]
    out = core.save_my_style(c, PID, 'manual notes', user_id=UID)
    assert out['saved'] is True and out['edits_absorbed'] == 0
    assert c.rpc_calls[0][1]['_edit_ids'] == []

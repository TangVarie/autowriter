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
    with pytest.raises(tools.InvalidInput) as refused:
        tools.save_my_style(PID, 'new observation only', edit_ids=[E1], _user_id=UID)
    # One sentence that names every step: where to get the task, what to merge, what to send back.
    msg = str(refused.value)
    assert msg.count('。') == 1 and msg.endswith('。'), msg
    for word in ('my_style', 'pending_distillation_task', 'existing_notes', 'expected_revision', 'edit_ids',
                 'save_my_style', '都没改'):
        assert word in msg, word
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


def test_an_rpc_error_that_is_not_a_missing_function_stays_an_error_and_writes_nothing():
    """Only "function not found" may fall back. A fault inside an existing function must surface."""
    for boom in (RuntimeError('synthetic RPC failure'),
                 RuntimeError('{"code":"42501","message":"permission denied for function deskcore_save_style_cas"}'),
                 RuntimeError('relation "autowriter.style_edits" does not exist')):
        c = CalibrationClient()
        def failing(_args, boom=boom):
            raise boom
        c.rpc_impl['deskcore_save_style_cas'] = failing
        with pytest.raises(RuntimeError) as err:
            core.save_my_style(c, PID, 'new notes', user_id=UID, edit_ids=[E1], expected_revision=REV)
        assert err.value is boom
        assert c.rows['user_calibration_notes'][0]['notes'] == 'old rule'
        assert c.rows['style_edits'][0]['distilled'] is False
        assert not any(q['op'] in ('upsert', 'update') for q in c.calls)


def test_before_021_is_applied_the_save_uses_the_previous_write_path_and_warns_once(monkeypatch, caplog):
    """The owner may run 021 after the deploy: until then save_my_style behaves as it did before 021."""
    monkeypatch.setattr(store, '_style_cas_missing_warned', False)
    c = CalibrationClient()                      # FakeClient answers an unknown RPC with PGRST202
    c.rows['style_edits'].append(dict(c.rows['style_edits'][0], id=E2))
    with caplog.at_level('WARNING', logger='deskcore.store'):
        out = core.save_my_style(c, PID, 'merged notes', user_id=UID, edit_ids=[E1], expected_revision=REV)
        again = core.save_my_style(c, PID, 'manual notes', user_id=UID)
    assert out['saved'] is True and out['edits_absorbed'] == 1 and 'revision' not in out
    assert out['pending_distillation'] == 1 and 'still_pending' in out
    assert [e['distilled'] for e in c.rows['style_edits']] == [True, False], 'only the listed edit is acknowledged'
    assert again['saved'] is True and again['edits_absorbed'] == 0 and 'note' in again
    assert [r['notes'] for r in c.rows['user_calibration_notes']] == ['manual notes'], 'one row, replaced in place'
    warned = [r for r in caplog.records if '021_style_distillation_cas.sql' in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelname == 'WARNING', 'one loud warning per process, not one per save'


@pytest.mark.parametrize('exc, missing', [
    (RuntimeError('PGRST202 Could not find the function in the schema cache'), True),
    (RuntimeError('{"code":"42883","message":"function autowriter.deskcore_save_style_cas(uuid) does not exist"}'), True),
    (RuntimeError('Could not find the function autowriter.deskcore_save_style_cas(_notes) in the schema cache'), True),
    (RuntimeError('relation "autowriter.style_edits" does not exist'), False),
    (RuntimeError('{"code":"PGRST205","message":"Could not find the table"}'), False),
    (RuntimeError('{"code":"42501","message":"permission denied"}'), False),
    (TimeoutError('timed out'), False),
    # the bare digits inside a request id or a UUID are not the error code
    (RuntimeError('permission denied; request id 0f42883a'), False),
    (RuntimeError('insert violates foreign key, key (project_id)=(a4288300-0000-4000-8000-000000000001)'), False),
    # 42883 about something else (a helper called inside the function body) is not "021 not applied"
    (RuntimeError('{"code":"42883","message":"function autowriter.some_helper(uuid) does not exist"}'), False),
])
def test_missing_function_is_detected_narrowly(exc, missing):
    assert store.style_cas_missing(exc) is missing


def test_missing_function_is_detected_from_the_error_code_attribute():
    class _ApiError(Exception):
        code = 'PGRST202'
    assert store.style_cas_missing(_ApiError('x')) is True
    _ApiError.code = '22023'
    assert store.style_cas_missing(_ApiError('notes must not be empty')) is False
    # a code that is present decides alone: the text may print a UUID or a request id containing 42883
    _ApiError.code = '23503'
    assert store.style_cas_missing(_ApiError(
        "{'code': '23503', 'details': 'Key (project_id)=(a4288300-0000-4000-8000-000000000001) is not present'}")) is False
    _ApiError.code = '42501'
    assert store.style_cas_missing(_ApiError('permission denied for function deskcore_save_style_cas "42883"')) is False
    _ApiError.code = '42883'
    assert store.style_cas_missing(_ApiError('function autowriter.deskcore_save_style_cas(uuid) does not exist')) is True
    assert store.style_cas_missing(_ApiError('operator does not exist: text = uuid')) is False


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


# ── The brief only reads the notes: a failed read degrades as it did before 021 ──────────────────────────

def test_a_failed_notes_read_does_not_fail_the_writing_brief(caplog):
    c = CalibrationClient(fail_read=True)
    c.rows['projects'][0]['calibration_notes'] = 'shared baseline'
    with caplog.at_level('ERROR', logger='deskcore.store'):
        brief = core.build_writing_brief(c, PID, user_id=UID, brief={})
    assert 'shared baseline' in brief['p1'] and 'old rule' not in brief['p1']
    assert any('read user calibration failed' in r.getMessage() for r in caplog.records), 'degraded, but logged'
    assert 'old rule' in core.build_writing_brief(c, PID, user_id=UID, brief={})['p1'], 'next call reads it again'


def test_the_brief_still_shows_a_note_that_has_no_revision():
    assert store.get_user_calibration(FakeClient(rows={'user_calibration_notes': [
        {'project_id': PID, 'user_id': UID, 'notes': 'old rule', 'updated_at': None}]}),
        PID, UID, strict=False) == ('old rule', None)


def test_a_failed_read_on_the_save_path_reaches_the_model_as_a_readable_refusal(monkeypatch, caplog):
    c = CalibrationClient(fail_read=True)
    monkeypatch.setattr(core, 'sb', lambda: c)
    with caplog.at_level('ERROR', logger='deskcore.store'):
        with pytest.raises(tools.InvalidInput, match='没有保存.*稍后原样再调一次 save_my_style'):
            tools.save_my_style(PID, 'manual notes', _user_id=UID)
    # the model gets one sentence; the cause (type, PostgREST code, stack) must be in the server log
    logged = [r for r in caplog.records if 'read user calibration failed' in r.getMessage()]
    assert len(logged) == 1 and logged[0].exc_info and logged[0].exc_info[1] is not None
    assert c.rpc_calls == [] and c.rows['user_calibration_notes'][0]['notes'] == 'old rule'


def test_a_failed_read_in_my_style_is_reported_not_shown_as_no_notes(monkeypatch):
    c = CalibrationClient(fail_read=True)
    monkeypatch.setattr(core, 'sb', lambda: c)
    monkeypatch.setattr(core.db, 'get_project', lambda *a, **k: {'id': PID, 'owner_id': UID})
    out = tools.my_style(PID, _user_id=UID)
    assert 'CalibrationReadError' in out['error'] and 'my_calibration' not in out


# ── What the removed CI step ("deskcore round-8 回归") asserted and this file did not ─────────────────────

E3 = str(uuid.UUID(int=3))
EDIT = {'ai_title': 'synthetic AI title', 'ai_body': 'synthetic AI body',
        'my_title': 'synthetic edited title', 'my_body': 'synthetic edited body', 'note': 'too exaggerated'}


def _three_pending():
    c = CalibrationClient()
    c.rows['style_edits'] = [dict(c.rows['style_edits'][0], id=i, **EDIT) for i in (E1, E2, E3)]
    return c


def _absorbing_rpc(c):
    """Stand-in for 021: acknowledge exactly the listed, still pending edits."""
    def rpc(args):
        hit = [e for e in c.rows['style_edits'] if e['id'] in args['_edit_ids'] and not e['distilled']]
        for e in hit:
            e['distilled'] = True
        c.rows['user_calibration_notes'][0]['notes'] = args['_notes']
        return [{'revision': '2026-11-14T10:05:00+00:00', 'edits_absorbed': len(hit)}]
    return rpc


def test_task_carries_the_material_and_the_server_side_instruction():
    c = _three_pending()
    task = core.build_distillation_task(c, PID, user_id=UID)
    for key in ('instruction', 'existing_notes', 'edits', 'edit_count', 'edit_ids', 'expected_revision'):
        assert key in task, key
    assert task['existing_notes'] == 'old rule' and task['edit_count'] == 3
    assert sorted(task['edit_ids']) == sorted([E1, E2, E3])
    assert 'synthetic edited title' in task['edits'] and 'too exaggerated' in task['edits']
    # The wording of the distillation comes from the server, not from each client.
    assert '铁律' in task['instruction'] and len(task['instruction']) > 100
    c.rows['style_edits'] = []
    assert core.build_distillation_task(c, PID, user_id=UID) is None, 'no edits, no task'


def test_record_edit_hands_over_the_task_and_says_the_step_is_not_finished():
    c = CalibrationClient()
    out = core.record_edit(c, PID, user_id=UID, **EDIT)
    assert out['stored'] is True and 'distillation_task' in out
    step = out['next_step']
    assert 'save_my_style' in step and 'edit_ids' in step and 'expected_revision' in step
    assert '白喂' in step or '没做完' in step
    assert out['distillation_task']['expected_revision'] == REV


@pytest.mark.parametrize('before_021', [False, True])
def test_only_the_listed_edits_are_acknowledged_and_the_rest_is_reported(monkeypatch, before_021):
    monkeypatch.setattr(store, '_style_cas_missing_warned', True)
    c = _three_pending()
    if not before_021:
        c.rpc_impl['deskcore_save_style_cas'] = _absorbing_rpc(c)
    out = core.save_my_style(c, PID, 'complete new notes\nsecond line', user_id=UID,
                             edit_ids=[E1, E2], expected_revision=REV)
    assert out['saved'] is True and out['lines'] == 2
    assert c.rows['user_calibration_notes'][0]['notes'] == 'complete new notes\nsecond line'
    assert {e['id'] for e in c.rows['style_edits'] if e['distilled']} == {E1, E2}
    assert out['edits_absorbed'] == 2 and out['pending_distillation'] == 1
    assert 'still_pending' in out, 'edits that are still pending must be said out loud'
    assert 'warning' not in out


@pytest.mark.parametrize('before_021', [False, True])
def test_a_manual_note_update_acknowledges_nothing_and_says_so(monkeypatch, before_021):
    monkeypatch.setattr(store, '_style_cas_missing_warned', True)
    c = _three_pending()
    if not before_021:
        c.rpc_impl['deskcore_save_style_cas'] = _absorbing_rpc(c)
    out = core.save_my_style(c, PID, 'manually rewritten notes', user_id=UID)
    assert not any(e['distilled'] for e in c.rows['style_edits'])
    assert out['edits_absorbed'] == 0 and out['pending_distillation'] == 3
    assert 'note' in out and 'still_pending' in out


def test_an_already_absorbed_edit_is_reported_as_a_warning():
    c = _three_pending()
    c.rows['style_edits'][0]['distilled'] = True
    c.rpc_impl['deskcore_save_style_cas'] = _absorbing_rpc(c)
    out = core.save_my_style(c, PID, 'notes', user_id=UID, edit_ids=[E1, E2], expected_revision=REV)
    assert out['edits_absorbed'] == 1 and 'warning' in out


@pytest.mark.parametrize('empty', ['', '   ', '\n\n', None])
def test_empty_notes_are_refused_before_any_read_or_write(empty):
    c = CalibrationClient()
    with pytest.raises(ValueError):
        core.save_my_style(c, PID, empty, user_id=UID)
    assert c.rpc_calls == [] and c.rows['user_calibration_notes'][0]['notes'] == 'old rule'


def test_the_break_between_the_two_steps_stays_visible():
    c = _three_pending()
    assert store.count_pending_distillation(c, PID, UID) == 3
    out = core.my_style(c, PID, user_id=UID)
    assert out['pending_distillation'] == 3
    task = out['pending_distillation_task']
    for key in ('instruction', 'existing_notes', 'edits', 'edit_ids', 'expected_revision'):
        assert key in task, key
    assert 'save_my_style' in out['next_step'] and 'expected_revision' in out['next_step']
    for e in c.rows['style_edits']:
        e['distilled'] = True
    assert store.count_pending_distillation(c, PID, UID) == 0
    clean = core.my_style(c, PID, user_id=UID)
    assert clean['pending_distillation'] == 0 and 'pending_distillation_task' not in clean
    assert 'pending_distillation' in (tools.my_style.__doc__ or ''), 'the tool text must teach the model to look'


def test_save_my_style_is_registered_needs_an_identity_and_does_not_swallow_errors():
    import inspect
    fn, needs_identity = tools.TOOLS['save_my_style'][:2]
    assert needs_identity is True
    assert '_safe(' not in inspect.getsource(fn), 'a write must not be wrapped in _safe'
    doc = tools.save_my_style.__doc__ or ''
    assert '完整' in doc, 'notes is the complete note, not an increment'
    assert 'edit_ids' in doc and 'expected_revision' in doc
    assert 'expected_revision' in inspect.signature(fn).parameters

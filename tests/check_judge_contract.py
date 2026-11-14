"""Synthetic cross-repository F02 acceptance; no source edits or external I/O.

Run after installing both repositories' local dependencies::

    python tests/check_judge_contract.py --judge-repo ../JevforCoentent
    python tests/check_judge_contract.py --verify-legacy-aw --output /tmp/f02.json

The default check needs no git history. The legacy-AW demonstration is optional
and requires the fixed historical commit to exist locally; it never fetches it.

The production AW HTTP client is bridged into the real Jev ASGI route. The paid
model, ledger persistence, and project/access database I/O use synthetic substitutes.
Responses traverse AW's real normalization, score row, next_step, and public writer
tools. Frozen baseline pure functions only demonstrate rolling-deployment ordering.
"""
from __future__ import annotations
import argparse
import ast
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from unittest.mock import patch

AW = Path(__file__).resolve().parents[1]
LEGACY_AW_COMMIT = '4a471b284151212206ba914eb7eaf6e42704dd2e'
BODY = '这是完全虚构的合成稿，只验证跨服务状态传递，不涉及任何客户、真实产品、账号或发布原文。' * 3
BLOCKED = []


def no_network(*args, **kwargs):
    BLOCKED.append('network_attempt')
    raise AssertionError('External networking is forbidden in this acceptance script')


def load_runtime(judge_repo):
    """Install the network fence before loading either repository's application code."""
    global A, JC, C, D, ASGI
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.create_connection = no_network
    os.environ['JUDGE_API_KEY'] = 'synthetic-f02-key'
    os.environ['JUDGE_WRITER_API_KEYS'] = ''
    for name in ('SUPABASE_URL', 'SUPABASE_SERVICE_ROLE_KEY', 'TIKHUB_API_KEY', 'ANTHROPIC_API_KEY',
                 'TYPESAFE_API_KEY', 'GOOGLE_API_KEY'):
        os.environ.pop(name, None)
    sys.path[:0] = [str(judge_repo), str(AW)]
    from fastapi.testclient import TestClient
    from judge import api
    import judge_client
    from deskcore import core
    from deskcore import tools
    A, JC, C, D = api, judge_client, core, tools
    ASGI = TestClient(A.app)  # In-memory ASGI transport; no listening socket.


class SyntheticModel:
    mock = True

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self.calls = 0
        self.seen = set()

    def call(self, body):
        self.calls += 1
        answers = {}
        for qid, spec in body['questions'].items():
            self.seen.add(qid)
            if spec['type'] == 'noul':
                answers[qid] = {'type': 'noul', 'noul': self.overrides.get(qid, 0.01)}
            else:
                # Select an actual supplied evidence sentence where required.
                choices = list(spec['criteria'])
                choice = next((k for k in choices if k != '没有'), choices[0])
                answers[qid] = {'type': 'choice', 'choice': choice,
                                'probabilities': {choice: 0.99}}
        return {'answers': answers, 'usage': {}}


ledger_writes = []
wire = []
active_model = SyntheticModel()
wire_transform = lambda x: x


def synthetic_write(rows):
    ledger_writes.append(len(rows))
    return len(rows), None


def bridge(url, *, headers, json, timeout):
    assert url == 'http://synthetic-judge.invalid/judge_draft'
    assert isinstance(json['write'], bool) and json['return_rows'] is False
    response = ASGI.post('/judge_draft', headers=headers, json=json)
    assert response.status_code == 200, (response.status_code, response.text)
    body = wire_transform(response.json())
    wire.append(body)
    class Reply:
        status_code = 200
        def json(self):
            return body
    return Reply()


def claims_all_passed(message):
    # Review wording may *negate* this same phrase, so do not forbid the words themselves.
    return '整批跟用户说一句「合规检查都过了」' in message


def run_case(name, overrides=None, transform=None):
    global active_model, wire_transform
    active_model = SyntheticModel(overrides)
    wire_transform = transform or (lambda x: x)
    payload = JC.build_draft_request(version_id='synthetic-version', title='合成标题', body=BODY,
                                     project='synthetic-project')
    result = JC.judge_draft(payload)
    row = C._judge_row({'index': 1, 'version_id': payload['subject_id']}, result)
    step = C._with_score_report(C.ARCHIVED_STEP_HEAD, {'results': [row]})
    return {'name': name, 'api': wire[-1], 'normalized': result, 'row': row,
            'step': step, 'model_calls': active_model.calls, 'seen': active_model.seen}


def legacy_wire(body):
    """Exactly the old response semantics for the negative-side ambiguity case."""
    out = copy.deepcopy(body)
    out.pop('hard_ambiguous', None)
    out.pop('needs_review', None)
    out['passed'] = not out['hard_fails'] and not out['unjudged'] and out.get('invalid_reason') is None
    return out


def legacy_positive_wire(body):
    """Synthetic old-wire positive-side ambiguity: old Jev also called it a hard fail."""
    out = legacy_wire(body)
    out['hard_fails'] = [['platform_health_v0.1', 'efficacy_claim', '是', 0.51, 'synthetic evidence']]
    out['plan'] = [{'kind': 'hard', 'bank': 'platform_health_v0.1', 'qid': 'efficacy_claim',
                    'p': 0.51, 'instruction': 'synthetic old-wire instruction'}]
    out['passed'] = False
    return out


def without_ambiguity_evidence(body):
    return {k: v for k, v in legacy_wire(body).items() if k not in ('ambiguous', 'hard_rules')}


def run_tool_cases():
    """Exercise both public writer tools through the full-response HTTP client."""
    global active_model, wire_transform
    scenarios = [
        ('new_jev_review', {'efficacy_claim': 0.49}, None, True),
        ('legacy_jev_negative_review', {'efficacy_claim': 0.49}, legacy_wire, True),
        ('legacy_jev_positive_review', {'efficacy_claim': 0.51}, legacy_positive_wire, True),
        ('clear', {}, None, False),
        ('legacy_missing_evidence', {}, without_ambiguity_evidence, None),
    ]
    checked = []
    # Only project access/context I/O is replaced; public tool, core adapter,
    # full-response client, and remote ASGI handler remain real.
    with patch.object(C, 'sb', lambda: object()), \
         patch.object(C, 'assert_project_access', lambda *a, **k: None), \
         patch.object(C, '_judge_project_context', lambda *a: {
             'project': 'synthetic-project', 'category': None, 'status': 'ok', 'detail': ''}):
        for name, overrides, transform, expected_review in scenarios:
            for tool_name in ('judge_draft', 'repair_plan_for'):
                active_model = SyntheticModel(overrides)
                wire_transform = transform or (lambda x: x)
                kwargs = {'_user_id': 'synthetic-user'}
                if tool_name == 'judge_draft':
                    kwargs['judge_paras'] = 'never'
                result = getattr(D, tool_name)('synthetic-desk-project', '合成标题', BODY, **kwargs)
                if expected_review is None:
                    assert result['judge_status'] != JC.JUDGE_OK
                else:
                    assert result['judge_status'] == JC.JUDGE_OK, result
                    assert result['needs_review'] is expected_review
                    assert result['passed'] is (not expected_review)
                    assert result['plan'] == [], 'Uncertainty must not turn into invented repair instructions'
                    if expected_review:
                        assert ['platform_health_v0.1', 'efficacy_claim'] in result['hard_ambiguous']
                    if tool_name == 'judge_draft':
                        assert result['hard_fails'] == []
                        assert 'ledger_rows' not in result
                checked.append({'scenario': name, 'tool': tool_name,
                                'judge_status': result['judge_status'], 'passed': result.get('passed'),
                                'needs_review': result.get('needs_review'),
                                'synthetic_model_calls': active_model.calls})
    return checked


def load_baseline_functions(repo, sha, rel, names, globals_):
    source = subprocess.run(['git', 'show', f'{sha}:{rel}'], cwd=repo, check=True,
                            capture_output=True, text=True).stdout
    tree = ast.parse(source)
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in nodes} == set(names)
    ns = dict(globals_)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), f'{rel}@{sha[:12]}', 'exec'), ns)
    return {name: ns[name] for name in names}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--judge-repo', type=Path, default=AW.parent / 'JevforCoentent',
                        help='Jev checkout (default: sibling JevforCoentent directory)')
    parser.add_argument('--output', type=Path, help='Also write the JSON summary to this file')
    parser.add_argument('--verify-legacy-aw', action='store_true',
                        help=f'Also demonstrate old-AW rollout risk using git commit {LEGACY_AW_COMMIT[:12]}')
    args = parser.parse_args(argv)
    judge_repo = args.judge_repo.expanduser().resolve()
    if not (judge_repo / 'judge/api.py').is_file():
        parser.error('--judge-repo must point to a Jev checkout containing judge/api.py')
    load_runtime(judge_repo)
    cases = []
    with patch.object(A, '_client', lambda: active_model), \
         patch.object(A, '_write_config_or_503', lambda: None), \
         patch.object(A, '_write_or_report', synthetic_write), \
         patch.object(JC.config, 'JUDGE_URL', 'http://synthetic-judge.invalid'), \
         patch.object(JC.config, 'JUDGE_API_KEY', 'synthetic-f02-key'), \
         patch.object(JC.requests, 'post', bridge), \
         patch.object(JC.telemetry, 'log_event', lambda *a, **k: None):
        clear = run_case('new_aw_new_jev_clear')
        assert clear['api']['passed'] and not clear['api']['needs_review']
        assert clear['row']['score'] == 100 and claims_all_passed(clear['step'])
        cases.append(clear)
        for p in (0.35, 0.49, 0.50, 0.51, 0.65):
            review = run_case(f'new_aw_new_jev_hard_ambiguity_{p}', {'efficacy_claim': p})
            assert review['api']['passed'] is False and review['api']['needs_review'] is True
            assert ['platform_health_v0.1', 'efficacy_claim'] in review['api']['hard_ambiguous']
            assert review['api']['hard_fails'] == [] and review['api']['plan'] == []
            assert review['row']['needs_review'] is True and review['row']['score'] < 100
            assert '需复核' in review['step'] and not claims_all_passed(review['step'])
            cases.append(review)
        description = run_case('new_aw_new_jev_description_ambiguity', {'own_experience': 0.49})
        assert 'own_experience' in description['seen'], 'Fixture must exercise a real non-hard descriptive question'
        assert description['api']['ambiguous'] and description['api']['hard_ambiguous'] == []
        assert description['api']['passed'] and description['row']['score'] == 100
        assert claims_all_passed(description['step'])
        cases.append(description)
        violation = run_case('new_aw_new_jev_clear_violation', {'efficacy_claim': 0.99})
        assert violation['api']['passed'] is False and violation['api']['hard_fails']
        assert not violation['api']['needs_review'] and violation['api']['plan']
        assert violation['row']['score'] < 100 and not claims_all_passed(violation['step'])
        cases.append(violation)
        rolling = run_case('new_aw_legacy_jev_negative_ambiguity', {'efficacy_claim': 0.49}, legacy_wire)
        assert rolling['api']['passed'] is True and 'hard_ambiguous' not in rolling['api']
        assert rolling['row']['needs_review'] and rolling['normalized']['passed'] is False
        assert '需复核' in rolling['step'] and not claims_all_passed(rolling['step'])
        cases.append(rolling)
        unknown = run_case('new_aw_legacy_response_without_ambiguity_evidence', transform=without_ambiguity_evidence)
        assert unknown['normalized']['judge_status'] != JC.JUDGE_OK
        assert not claims_all_passed(unknown['step'])
        cases.append(unknown)
        tool_cases = run_tool_cases()

    legacy_demonstrated = False
    if args.verify_legacy_aw:
        # Optional: the normal integration check does not require any git history.
        old_client = load_baseline_functions(AW, LEGACY_AW_COMMIT,
                                            'judge_client.py', ['_normalize_ok'], JC.__dict__)
        old_core = load_baseline_functions(AW, LEGACY_AW_COMMIT,
                                          'deskcore/core.py', ['draft_score', '_score_report_step'], C.__dict__)
        response = cases[2]['api']
        normalized = old_client['_normalize_ok'](response)
        scored = old_core['draft_score'](dict(normalized, judge_status='ok'))
        old_step = old_core['_score_report_step']({'results': [dict(scored, index=1, judge_status='ok')]})
        assert response['passed'] is False and scored['score'] == 100 and claims_all_passed(old_step)
        legacy_demonstrated = True
    assert not BLOCKED, 'Unexpected outbound connection attempt was blocked'
    summary = {'network_calls': 0, 'paid_calls': 0, 'synthetic_ledger_write_calls': len(ledger_writes),
               'cases': [{'name': r['name'], 'judge_status': r['normalized']['judge_status'],
                          'api_passed': r['api']['passed'], 'score': r['row'].get('score'),
                          'needs_review': r['row'].get('needs_review', False),
                          'claims_all_passed': claims_all_passed(r['step']),
                          'synthetic_model_calls': r['model_calls']} for r in cases],
               'writer_tool_cases': tool_cases,
               'rolling_deployment': {'new_aw_legacy_jev_wire_safe': True,
                                      'old_aw_new_jev_risk_demonstrated': legacy_demonstrated,
                                      'legacy_aw_commit': LEGACY_AW_COMMIT if legacy_demonstrated else None,
                                      'required_order': ['AW first', 'Jev second']},
               'scope': 'Real ASGI route + AW score and public writer tool consumers; synthetic model, ledger and project I/O; no commit/DB integration'}
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + '\n', encoding='utf-8')
    print(rendered)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

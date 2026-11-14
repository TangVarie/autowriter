"""F02: uncertainty about a hard rule must survive HTTP normalization and final wording."""
from __future__ import annotations

import pytest

import judge_client as J
from deskcore import core


def payload(**fields):
    out = {'passed': True, 'hard_fails': [], 'plan': [], 'unjudged': [], 'written': 4,
           'hard_ambiguous': [['platform', 'claim']], 'needs_review': True}
    out.update(fields)
    if 'needs_review' not in fields:
        out['needs_review'] = bool(out['hard_ambiguous'])
    return out


def final_row(body):
    normalized = J._normalize_ok(body)
    assert normalized is not None
    result = J.result(J.JUDGE_OK, **normalized)
    return core._judge_row({'index': 0, 'version_id': 'synthetic-version'}, result)


def test_hard_ambiguity_changes_score_and_prevents_an_all_pass_reply():
    row = final_row(payload())
    assert row['hard_ambiguous'] == [['platform', 'claim']] and row['needs_review'] is True
    assert row['passed'] is False and row['score'] == 92 and row['fixes'] == []
    text = core._score_report_step({'results': [row], 'skipped': []})
    assert '需要人工复核' in text and '自己照各自的 fixes 改一轮' not in text
    assert '整批跟用户说一句「合规检查都过了」' not in text


def test_legacy_judge_is_supported_by_intersecting_ambiguity_with_hard_rules():
    old = payload(ambiguous=[['platform', 'claim'], ['fq', 'description']],
                  hard_rules={'platform:claim': '否'})
    del old['hard_ambiguous']
    del old['needs_review']
    row = final_row(old)
    assert row['hard_ambiguous'] == [['platform', 'claim']] and row['score'] == 92


def test_ordinary_descriptive_ambiguity_does_not_create_a_hard_penalty():
    old = payload(ambiguous=[['fq', 'description']], hard_rules={'platform:claim': '否'})
    del old['hard_ambiguous']
    del old['needs_review']
    row = final_row(old)
    assert row['score'] == 100 and not row.get('needs_review') and not row.get('hard_ambiguous')
    assert '整批跟用户说一句「合规检查都过了」' in core._score_report_step({'results': [row]})


def test_legacy_ambiguous_fail_is_not_double_penalized_or_given_an_unsupported_rewrite():
    old = payload(hard_fails=[['platform', 'claim', '是', .51]],
                  plan=[{'kind': 'hard', 'bank': 'platform', 'qid': 'claim', 'p': .51, 'instruction': 'synthetic rewrite'}],
                  ambiguous=[['platform', 'claim']], hard_rules={'platform:claim': '否'})
    del old['hard_ambiguous']
    row = final_row(old)
    assert row['score'] == 92 and row['fixes'] == [] and not row.get('more_fixes') and not row.get('hard_fails')
    assert '需要人工复核' in core._score_report_step({'results': [row]})


@pytest.mark.parametrize('extra', [
    {}, {'ambiguous': []}, {'hard_rules': {}}, {'hard_ambiguous': None},
    {'hard_ambiguous': [['platform']]}, {'ambiguous': [['platform']], 'hard_rules': {}},
])
def test_missing_or_malformed_ambiguity_evidence_is_not_silently_clean(extra):
    body = payload()
    del body['hard_ambiguous']
    body.update(extra)
    assert J._normalize_ok(body) is None


def test_a_mixed_batch_reports_review_and_keeps_clean_drafts_separate():
    review = final_row(payload())
    clean = dict(final_row(payload(hard_ambiguous=[])), index=1)
    text = core._score_report_step({'results': [review, clean]})
    assert '需要人工复核' in text and '另外 1 篇没查出要改的地方' in text
    assert '整批跟用户说一句「合规检查都过了」' not in text


def test_an_explicit_review_flag_cannot_be_discarded_because_the_list_is_empty():
    assert J._normalize_ok(payload(hard_ambiguous=[], needs_review=True)) is None

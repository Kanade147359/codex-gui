import json

from test_real_provider_review import review_report


def test_child_review_presentation_preserves_findings_and_unverified_scope():
    report = dict(reviewed_paths=['app/providers.py'], findings=[dict(priority='P1', issue='must retain')],
                  unverified=['missing interface'])
    assert review_report('Report follows.\n```json\n' + json.dumps(report) + '\n```\nEnd.') == report


def test_ambiguous_or_duplicate_child_reports_are_rejected():
    report = json.dumps(dict(reviewed_paths=[], findings=[], unverified=[]))
    assert review_report(f'```json\n{report}\n```\n```json\n{report}\n```') is None
    assert review_report('{"reviewed_paths":[],"findings":[1],"findings":[],"unverified":[]}') is None
    assert review_report('ordinary prose') is None

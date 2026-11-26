import json

from test_real_provider_review import codex_v2_report, review_report


def test_child_review_presentation_preserves_findings_and_unverified_scope():
    report = dict(reviewed_paths=['app/providers.py'], findings=[dict(priority='P1', issue='must retain')],
                  unverified=['missing interface'])
    assert review_report('Report follows.\n```json\n' + json.dumps(report) + '\n```\nEnd.') == report


def test_ambiguous_or_duplicate_child_reports_are_rejected():
    report = json.dumps(dict(reviewed_paths=[], findings=[], unverified=[]))
    assert review_report(f'```json\n{report}\n```\n```json\n{report}\n```') is None
    assert review_report('{"reviewed_paths":[],"findings":[1],"findings":[],"unverified":[]}') is None
    assert review_report('ordinary prose') is None


def test_native_codex_report_requires_completed_child_lineage(tmp_path):
    identity = '01a21894-c159-76b2-8f58-965d97a5b104'
    session = tmp_path / 'sessions' / ('rollout-' + identity + '.jsonl')
    session.parent.mkdir()
    report = dict(reviewed_paths=['app/providers.py'], findings=[], unverified=[])
    meta = dict(id=identity, cwd='/review', source=dict(subagent=dict(thread_spawn=dict(parent_thread_id='parent', depth=1))))
    rows = [dict(type='session_meta', payload=meta), dict(type='event_msg', payload=dict(
        type='agent_message', phase='commentary', message=json.dumps(report)))]
    def save():
        session.write_text('\n'.join(json.dumps(row) for row in rows))
    save()
    assert codex_v2_report(tmp_path, identity, 'parent', '/review') is None
    rows[-1]['payload']['phase'] = 'final_answer'
    save()
    assert codex_v2_report(tmp_path, identity, 'parent', '/review') == report
    assert codex_v2_report(tmp_path, identity, 'other-parent', '/review') is None
    assert codex_v2_report(tmp_path, identity, 'parent', '/other-worktree') is None

import json
import subprocess
import sys
import pytest

from test_real_provider_review import codex_v2_report, codex_read_paths, diff_read_fixture, qualify_partitions, review_report


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
    rows = [dict(type='session_meta', payload=meta), dict(type='response_item', payload=dict(
        type='message', role='assistant', phase='commentary', content=[dict(type='output_text', text=json.dumps(report))]))]
    def save():
        session.write_text('\n'.join(json.dumps(row) for row in rows))
    save()
    assert codex_v2_report(tmp_path, identity, 'parent', '/review') is None
    rows[-1]['payload']['phase'] = 'final_answer'
    save()
    assert codex_v2_report(tmp_path, identity, 'parent', '/review') == report
    assert codex_v2_report(tmp_path, identity, 'other-parent', '/review') is None
    assert codex_v2_report(tmp_path, identity, 'parent', '/other-worktree') is None


def test_each_review_partition_needs_a_distinct_completed_agent():
    partitions = dict(backend=['app/providers.py'], frontend=['static/providers.js'])
    def report(paths):
        return dict(reviewed_paths=paths, findings=[], unverified=[])
    complete = report(['app/providers.py', 'static/providers.js'])
    with pytest.raises(AssertionError, match='Distinct completed'):
        qualify_partitions(partitions, dict(first=complete, second=report([])))
    with pytest.raises(AssertionError, match='Distinct completed'):
        qualify_partitions(partitions, dict(same_agent=complete))
    assert qualify_partitions(partitions, dict(first=report(['app/providers.py']),
        second=report(['static/providers.js']))) == dict(backend='first', frontend='second')


def test_declared_scope_or_commentary_cannot_replace_observed_diff_reads(tmp_path):
    identity = '01a21894-c159-76b2-8f58-965d97a5b104'
    session = tmp_path / 'sessions' / ('rollout-' + identity + '.jsonl')
    session.parent.mkdir()
    pages = {'app/providers.py': ['BEGIN pinned page 0\nactual code\nEND page 0', 'BEGIN page 1\nmore code\nEND page 1']}
    meta = dict(id=identity, cwd='/review', source=dict(subagent=dict(thread_spawn=dict(parent_thread_id='parent', depth=1))))
    report = dict(reviewed_paths=list(pages), findings=[], unverified=[])
    rows = [dict(type='session_meta', payload=meta), dict(type='response_item', payload=dict(
        type='message', role='assistant', phase='final_answer', content=[dict(type='output_text', text=json.dumps(report))])),
        dict(type='response_item', payload=dict(type='message', role='assistant', phase='commentary',
             content=[dict(type='output_text', text='\n'.join(pages['app/providers.py']))]))]
    def save():
        session.write_text('\n'.join(json.dumps(row) for row in rows))
    save()
    assert codex_read_paths(tmp_path, identity, 'parent', '/review', pages) == []
    rows.append(dict(type='response_item', payload=dict(type='function_call_output', output=pages['app/providers.py'][0])))
    save()
    assert codex_read_paths(tmp_path, identity, 'parent', '/review', pages) == []
    rows.append(dict(type='response_item', payload=dict(type='custom_tool_call_output',
         output=json.dumps({'content':[{'type':'text','text':pages['app/providers.py'][1]}]}))))
    save()
    assert codex_read_paths(tmp_path, identity, 'parent', '/review', pages) == list(pages)
    assert codex_read_paths(tmp_path, identity, 'other-parent', '/review', pages) == []


def test_trusted_diff_reader_returns_the_pinned_patch_without_edits(tmp_path, git_repo):
    base = subprocess.check_output(['git', '-C', str(git_repo), 'rev-parse', 'HEAD'], text=True).strip()
    (git_repo / 'review.py').write_text('VALUE = 42\n')
    subprocess.run(['git', '-C', str(git_repo), 'add', 'review.py'], check=True)
    subprocess.run(['git', '-C', str(git_repo), 'commit', '-qm', 'review witness'], check=True)
    head = subprocess.check_output(['git', '-C', str(git_repo), 'rev-parse', 'HEAD'], text=True).strip()
    helper, pages = diff_read_fixture(tmp_path, git_repo, base, head, ['review.py'])
    output = subprocess.check_output([sys.executable, '-B', str(helper), 'review.py', '0'], text=True)
    assert output == pages['review.py'][0]
    assert '+VALUE = 42' in output and 'BEGIN-PR19-DIFF ' + head + ':' in output
    assert not subprocess.check_output(['git', '-C', str(git_repo), 'status', '--porcelain'])

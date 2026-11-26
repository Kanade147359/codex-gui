#!/usr/bin/env python3
"""Offline Claude wire protocol fixture. Never loads credentials or calls a service."""
import json
import os
from pathlib import Path
import sys
import time
import uuid

AUTH = True
BILLING = 'subscription'
if '--version' in sys.argv:
    print('2.1.185 (Claude Code fake)')
    sys.exit(0)
if '--help' in sys.argv:
    print('''--resume --output-format --include-partial-messages --json-schema --input-format
--permission-mode <mode> (choices: "dontAsk", "acceptEdits", "plan", "auto", "default")
--plugin-dir
--effort <level> Effort level for the current session
 (low, medium, high, xhigh, max)
--setting-sources --settings --strict-mcp-config --safe-mode --append-system-prompt''')
    sys.exit(0)
if sys.argv[1:3] == ['auth', 'status']:
    print(json.dumps(dict(loggedIn=AUTH, authMethod='claude.ai' if BILLING == 'subscription' else 'api_key',
                          apiProvider='firstParty', subscriptionType='pro')))
    sys.exit(0 if AUTH else 1)

def emit(value):
    print(json.dumps(value), flush=True)

message = json.loads(sys.stdin.readline())
content = message['message']['content']
prompt = content[0]['text']
command = prompt.splitlines()[0]
resuming = '--resume' in sys.argv
session = sys.argv[sys.argv.index('--resume') + 1] if resuming else str(uuid.uuid4())
git_meta = Path(Path('.git').read_text().split(':', 1)[1].strip())
fixture_dir = git_meta / 'fake-claude'
fixture_dir.mkdir(exist_ok=True)
state = fixture_dir / 'sessions.json'
saved = json.loads(state.read_text()) if state.exists() else {}
if resuming and (session not in saved or command == 'missing-session'):
    print('No conversation found with session ID', file=sys.stderr)
    sys.exit(1)
if command == 'wrong-session':
    session = str(uuid.uuid4())
saved[session] = saved.get(session, 0) + 1
state.write_text(json.dumps(saved))
with (fixture_dir / 'calls.jsonl').open('a') as f:
    f.write(json.dumps(dict(session=session, prompt=prompt, images=len(content)-1, resume=resuming)) + '\n')
emit(dict(type='system', subtype='init', session_id=session, model='fake-claude', apiKeySource='none'))
emit(dict(type='future-event', payload={'new': True}))
print('{invalid json', flush=True)
if command in ('sleep', 'sleep-ignore'):
    if command == 'sleep-ignore':
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
if command == 'orphan':
    import subprocess
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    (fixture_dir / 'child.pid').write_text(str(child.pid))
if command == 'quota':
    emit(dict(type='rate_limit_event', rate_limit_info=dict(status='rejected', isUsingOverage=False)))
    sys.exit(0)
if command == 'overage':
    emit(dict(type='rate_limit_event', rate_limit_info=dict(status='allowed', isUsingOverage=True)))
    sys.exit(0)
if command == 'duplicate-fields':
    print('{"type":"result","structured_output":{"status":"PARTIAL","status":"SUCCESS","reason":"ambiguous"}}', flush=True)
    sys.exit(0)
if command == 'auth-expired':
    emit(dict(type='system', subtype='api_error', error=dict(type='authentication_error')))
    sys.exit(1)
if command == 'crash':
    emit(dict(type='assistant', message=dict(content=[dict(type='text', text='Starting')])) )
    os._exit(17)
if command == 'stderr':
    print('fake diagnostic', file=sys.stderr, flush=True)
if command == 'no-result':
    sys.exit(0)
if command == 'bad-shape':
    emit(dict(type='assistant', message=5))
    sys.exit(0)
emit(dict(type='stream_event', event=dict(type='content_block_delta', delta=dict(type='text_delta', text='Implementing'))))
emit(dict(type='assistant', message=dict(content=[dict(type='tool_use', id='tool-1', name='Read', input=dict(file_path='README.md'))])))
emit(dict(type='user', message=dict(content=[dict(type='tool_result', tool_use_id='tool-1', content='file contents')])))
if command == 'subagents':
    emit(dict(type='system', subtype='task_started', task_id='sub-1', description='Inspect code'))
    emit(dict(type='assistant', parent_tool_use_id='sub-1', message=dict(content=[dict(type='text', text='Inspecting')])) )
    emit(dict(type='system', subtype='task_notification', task_id='sub-1', status='completed', description='Done'))
bundle_path = Path('.autopilot-inputs/requirements.json')
if bundle_path.exists():
    bundle = json.loads(bundle_path.read_text())
    node = bundle['node']
    Path(node + '.txt').write_text('bad\n' if node == 'A' and not resuming else 'good\n')
status = {'blocked':'BLOCKED', 'needs-input':'NEEDS_INPUT', 'partial':'PARTIAL'}.get(command, 'SUCCESS')
result = dict(type='result', subtype='success', is_error=False, session_id=session,
              structured_output=dict(status=status, reason='fake CLI result'), duration_ms=42,
              usage=dict(input_tokens=12, output_tokens=8, cache_read_input_tokens=30, cache_creation_input_tokens=2))
if command == 'permission':
    result['permission_denials'] = [dict(tool_name='Bash')]
if command == 'no-structured':
    result.pop('structured_output')
if command == 'terminal-json':
    result['result'] = json.dumps(result.pop('structured_output'))
emit(result)
if command == 'duplicate-result':
    emit(result)

"""Real subscription-only CLI E2E. Never runs from the default suite.

Set BOTH flags only after independently verifying extra usage/credits disabled:
CODEX_GUI_REAL_CLAUDE=1 CODEX_GUI_CLAUDE_EXTRA_USAGE_DISABLED=1 pytest tests/test_real_claude.py
"""
import asyncio
import os
from pathlib import Path
import shutil

import pytest

from app.providers import ClaudeCodeProvider, session_id
from test_providers import finished

pytestmark = pytest.mark.skipif(
    os.environ.get('CODEX_GUI_REAL_CLAUDE') != '1' or os.environ.get('CODEX_GUI_CLAUDE_EXTRA_USAGE_DISABLED') != '1',
    reason='NOT_RUN: real CLI needs explicit opt-in and verified no-extra-usage billing',
)


@pytest.fixture
def isolated_claude_account(tmp_path, monkeypatch):
    """Stage official OAuth credentials privately; never change the owner's CLI state."""
    source = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
    credential = source / '.credentials.json'
    if not credential.is_file():
        pytest.skip('NOT_RUN: file-based Claude OAuth unavailable for isolated execution')
    config = tmp_path / 'claude-account'
    config.mkdir(mode=0o700)
    staged = config / '.credentials.json'
    staged.write_bytes(credential.read_bytes())
    staged.chmod(0o600)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
    try:
        yield config
    finally:
        shutil.rmtree(config)


def test_real_subscription_new_and_resumed_session(git_repo, make_manager, isolated_claude_account):
    m = make_manager()
    async def scenario():
        info = await ClaudeCodeProvider(m.settings.claude_bin).describe()
        if not info['available'] or info['billing'] != 'subscription':
            pytest.skip('NOT_RUN: installed CLI subscription route cannot be confirmed')
        m.providers.save('claude', dict(billing_mode='subscription', extra_usage_disabled_confirmed=True))
        try:
            task = await m.create_task(repository=str(git_repo), prompt='Return SUCCESS with reason subscription smoke; do not call tools or edit files.',
                                      provider='claude', model=os.environ.get('CODEX_GUI_REAL_CLAUDE_MODEL', 'sonnet'),
                                      provider_options=dict(trusted_repository=True, permission_mode='dontAsk', timeout_seconds=180), auto_retry=False)
            first = await finished(m, task, timeout=210)
            assert first['status'] == 'completed' and first['task_outcome'] == 'success', first['status_detail']
            await m.send_instruction(task['id'], 'Return SUCCESS with reason resumed subscription smoke. Do not call tools.')
            second = await finished(m, task, timeout=210)
            assert second['task_outcome'] == 'success' and session_id(first) == session_id(second), second['status_detail']
            print(f"real Claude: initial/resume PASS; session={session_id(second)}; model={second['model']}")
        finally:
            await m.shutdown()
    asyncio.run(scenario())

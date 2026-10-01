#!/usr/bin/env bash
# Start Codex GUI on http://127.0.0.1:8765 (creates .venv on first run).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c "import fastapi, uvicorn, jinja2" 2>/dev/null; then
  .venv/bin/python -m pip install -q -r requirements.txt
fi

# ---- SSH agent (WSL) ------------------------------------------------------
# Reuse a working agent if there is one; otherwise start one and remember it in
# ~/.ssh/agent.env so the next ./run.sh (and any shell that sources it) shares it.
# The exports below are inherited by uvicorn -> codex / git subprocesses.
SSH_AGENT_ENV="${CODEX_GUI_SSH_AGENT_ENV:-$HOME/.ssh/agent.env}"
SSH_KEY="${CODEX_GUI_SSH_KEY:-$HOME/.ssh/id_ed25519}"

# ssh-add -l exit codes: 0 = agent has keys, 1 = agent reachable but empty, 2 = no agent.
agent_reachable() {
  local rc=0
  ssh-add -l >/dev/null 2>&1 || rc=$?
  [ "$rc" -ne 2 ]
}

setup_ssh_agent() {
  if ! command -v ssh-agent >/dev/null 2>&1 || ! command -v ssh-add >/dev/null 2>&1; then
    echo "ssh: ssh-agent/ssh-add not found (install openssh-client); skipping SSH agent setup" >&2
    return 0
  fi

  # 1. Agent already in this environment (inherited shell, forwarding, ...).
  if [ -n "${SSH_AUTH_SOCK:-}" ] && agent_reachable; then
    echo "ssh: using existing agent ($SSH_AUTH_SOCK)"
  else
    # 2. Agent saved by a previous run.
    if [ -r "$SSH_AGENT_ENV" ]; then
      # shellcheck disable=SC1090
      . "$SSH_AGENT_ENV" >/dev/null
    fi
    if [ -n "${SSH_AUTH_SOCK:-}" ] && agent_reachable; then
      echo "ssh: reusing saved agent ($SSH_AUTH_SOCK, pid ${SSH_AGENT_PID:-?})"
    else
      # 3. Stale or missing: start a new one (only now).
      mkdir -p "$(dirname "$SSH_AGENT_ENV")"
      if ( umask 077; ssh-agent -s | sed 's/^echo /# echo /' > "$SSH_AGENT_ENV" ); then
        chmod 600 "$SSH_AGENT_ENV"
        # shellcheck disable=SC1090
        . "$SSH_AGENT_ENV" >/dev/null
        echo "ssh: started new agent ($SSH_AUTH_SOCK, pid ${SSH_AGENT_PID:-?}); saved to $SSH_AGENT_ENV"
      else
        rm -f "$SSH_AGENT_ENV"
        unset SSH_AUTH_SOCK SSH_AGENT_PID
        echo "ssh: could not start ssh-agent; continuing without SSH authentication" >&2
        return 0
      fi
    fi
  fi
  export SSH_AUTH_SOCK
  [ -z "${SSH_AGENT_PID:-}" ] || export SSH_AGENT_PID

  # Register the default key only if the agent holds no key at all. A passphrase is asked
  # by the normal ssh-add prompt, which needs a terminal.
  local rc=0
  ssh-add -l >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 1 ]; then
    if [ ! -f "$SSH_KEY" ]; then
      echo "ssh: no keys in agent and $SSH_KEY does not exist; run ssh-add <key> yourself" >&2
    elif ssh-add "$SSH_KEY"; then
      :
    else
      echo "ssh: ssh-add $SSH_KEY failed; git over SSH will not work until a key is added" >&2
    fi
  fi
}
setup_ssh_agent

HOST="${CODEX_GUI_HOST:-127.0.0.1}"
PORT="${CODEX_GUI_PORT:-8765}"

# ---- Login ----------------------------------------------------------------
# Anyone who can sign in can run Codex (and so commands) on this machine, so the login is on by default and
# the server refuses to listen on a non-loopback address without it.
if [ "${CODEX_GUI_AUTH:-1}" = "0" ]; then
  case "$HOST" in
    127.0.0.1|localhost|::1) echo "auth: login is DISABLED (CODEX_GUI_AUTH=0); only this machine can reach the GUI" >&2 ;;
    *) echo "error: CODEX_GUI_AUTH=0 with CODEX_GUI_HOST=$HOST would expose Codex without a login. Refusing to start." >&2; exit 1 ;;
  esac
elif [ "$(.venv/bin/python -m app.users count)" = "0" ]; then
  if [ -t 0 ]; then
    read -r -p "No login user yet. Username to create: " NEW_USER
    .venv/bin/python -m app.users add "$NEW_USER"
  else
    echo "auth: no users yet. Run: .venv/bin/python -m app.users add <username>" >&2
  fi
fi

echo "Codex GUI: http://${HOST}:${PORT}"
exec .venv/bin/python -m uvicorn --factory app.main:create_app --host "$HOST" --port "$PORT"

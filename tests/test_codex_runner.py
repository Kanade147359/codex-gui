import json

from app.codex_runner import CodexRunner, parse_line
from app.logstore import TaskLog, read_log


def task(**over):
    t = dict(worktree="/wt/x", prompt="do it -- --dangerously-bypass-approvals-and-sandbox", model="",
             reasoning_effort="default", auto_approval=1)
    t.update(over)
    return t


def test_default_command():
    cmd = CodexRunner("codex").build_command(task())
    assert cmd == ["codex", "exec", "--json", "-C", "/wt/x", "--approve-for-me", "-"]


def test_prompt_never_in_argv_and_no_dangerous_flag():
    for t in (task(), task(auto_approval=0), task(model="m", reasoning_effort="high")):
        cmd = CodexRunner().build_command(t)
        assert not any("dangerously" in a for a in cmd)
        assert t["prompt"] not in cmd
        assert cmd[-1] == "-"  # prompt is read from stdin


def test_resume_command_keeps_options_and_continues_the_thread():
    t = task(model="gpt-x", reasoning_effort="high")
    fresh = CodexRunner("codex").build_command(t)
    resumed = CodexRunner("codex").build_command(t, "01a0f766-f849-7ef2-92a5-a6d01e361b64")
    # same options in front, then `resume <thread> -` (the order verified against codex-cli 0.159.2)
    assert resumed == fresh[:-1] + ["resume", "01a0f766-f849-7ef2-92a5-a6d01e361b64", "-"]
    assert resumed[:5] == ["codex", "exec", "--json", "-C", "/wt/x"]
    assert t["prompt"] not in resumed


def test_never_passes_daemon_flags():
    for resume in (None, "tid"):
        assert not any("daemon" in a for a in CodexRunner().build_command(task(), resume))


def test_auto_approval_off_omits_flag():
    assert "--approve-for-me" not in CodexRunner().build_command(task(auto_approval=0))


def test_model_and_effort():
    cmd = CodexRunner().build_command(task(model="gpt-x", reasoning_effort="high"))
    assert cmd[cmd.index("--model") + 1] == "gpt-x"
    assert cmd[cmd.index("-c") + 1] == 'model_reasoning_effort="high"'
    plain = CodexRunner().build_command(task())
    assert "--model" not in plain and "-c" not in plain  # "default" passes nothing


def test_parse_agent_message():
    line = json.dumps({"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": "hi there"}})
    p = parse_line(line + "\n")
    assert p["type"] == "item.completed/agent_message" and p["message"] == "hi there"
    assert p["event"]["item"]["text"] == "hi there"


def test_parse_command_execution():
    line = json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "ls",
                       "aggregated_output": "a\nb\n", "exit_code": 0, "status": "completed"}})
    p = parse_line(line)
    assert p["message"].startswith("$ ls") and "(exit 0)" in p["message"] and "a\nb" in p["message"]


def test_parse_other_events():
    assert parse_line('{"type":"thread.started","thread_id":"abc"}')["message"] == "thread abc"
    assert "input_tokens=5" in parse_line('{"type":"turn.completed","usage":{"input_tokens":5}}')["message"]
    assert parse_line('{"type":"turn.failed","error":{"message":"boom"}}')["message"] == "boom"
    assert parse_line('{"type":"error","message":"bad"}')["message"] == "bad"
    assert parse_line('{"type":"turn.started"}')["message"] == ""


def test_unparseable_lines_are_kept_raw():
    for line in ("plain text\n", "{not json}\n", "[1, 2]\n", '"str"\n', "\n", '{"truncated": '):
        p = parse_line(line)
        assert p["type"] == "raw" and p["event"] is None
        assert p["message"] == line.rstrip("\n")


def test_long_message_is_clipped_but_event_kept_whole():
    big = "x" * 10000
    p = parse_line(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": big}}))
    assert len(p["message"]) < 2100 and "+8000 chars" in p["message"]
    assert p["event"]["item"]["text"] == big


def test_task_log_roundtrip_and_offsets(tmp_path):
    path = tmp_path / "t.jsonl"
    log = TaskLog(path)
    log.add_system("hello")
    log.add_stdout('{"type":"turn.started"}\n')
    log.add_stdout("not json\n")
    log.add_stderr("oops\n")
    entries, off = read_log(path, 0)
    assert [(e["stream"], e["type"]) for e in entries] == [
        ("system", "system"), ("stdout", "turn.started"), ("stdout", "raw"), ("stderr", "stderr")]
    assert entries[2]["message"] == "not json" and entries[1]["event"] == {"type": "turn.started"}
    assert read_log(path, off) == ([], off)  # nothing new
    log.add_system("later")
    more, off2 = read_log(path, off)
    assert [e["message"] for e in more] == ["later"] and off2 > off
    log.close()


def test_read_log_ignores_partial_trailing_line(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text('{"ts":"","stream":"system","type":"system","message":"a","event":null}\n{"ts":"","str')
    entries, off = read_log(path, 0)
    assert len(entries) == 1 and off == path.read_text().index("\n") + 1
    assert read_log(tmp_path / "missing.jsonl", 0) == ([], 0)

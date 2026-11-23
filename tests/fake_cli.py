#!/usr/bin/env python3
"""A local app-server fixture for testing without credentials or network."""

import base64
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def hold_process_lock(lock_path):
    lock = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], pass_fds=(lock,))


if lock_path := os.environ.get("AGENT_PROCESS_LOCK"):
    hold_process_lock(lock_path)


def emit(**event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def notice(method, **params):
    emit(method=method, params={"threadId": "test-thread", **params})


expected_images = json.loads(os.environ.get("EXPECTED_IMAGES", "[]"))

if "--print" in sys.argv:
    assert "--restricted" in sys.argv
    assert sys.argv[sys.argv.index("--input-format") + 1] == "stream-json"
    session_id = "test-claude"
    emit(type="system", subtype="init", session_id=session_id, model="fake-claude")
    for index, line in enumerate(sys.stdin):
        request = json.loads(line)
        assert request["type"] == "user"
        assert request["session_id"] == ("default" if index == 0 else session_id)
        expected = os.environ["EXPECTED_PROMPT" if index == 0 else "EXPECTED_FOLLOW_UP"]
        content = request["message"]["content"]
        if expected_images and index == 0:
            assert content == [
                *([{"type": "text", "text": expected}] if expected else []),
                *({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
                  for data in expected_images),
            ]
        else:
            assert content == expected
        emit(type="stream_event", event={"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "thinking_delta", "thinking": "PRIVATE_REASONING"}})
        tool = {"type": "tool_use", "id": "read-claude", "name": "Read", "input": {"file_path": "activity.txt"}}
        emit(type="stream_event", event={"type": "content_block_start", "index": 1, "content_block": tool})
        emit(type="assistant", message={"content": [tool]})
        emit(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "read-claude", "is_error": False}]})
        reply = "claude-first" if index == 0 else "claude-with-context"
        emit(type="result", is_error=False, session_id=session_id, result=reply)
    raise SystemExit(0)


if "--fake-doubao" in sys.argv:
    resumed = "--resume" in sys.argv
    if resumed:
        assert sys.argv[sys.argv.index("--resume") + 1] == "123456"
    expected = os.environ["EXPECTED_FOLLOW_UP" if resumed else "EXPECTED_PROMPT"]
    assert sys.stdin.read() == expected
    tool = {"type": "tool_use", "id": "read-doubao", "name": "Read", "input": {"file_path": "activity.txt"}}
    emit(type="assistant", message={"content": [tool]})
    emit(type="user", message={"content": [{"type": "tool_result", "tool_use_id": "read-doubao", "is_error": True}]})
    reply = {"content": "doubao-with-context" if resumed else "doubao-first"}
    emit(type="result", is_error=False, session_id="123456", result=reply)
    raise SystemExit(0)


model = None
turns = 0
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        emit(id=request["id"], result={})
    elif method == "model/list":
        if Path(sys.argv[0]).name == "traex":
            models = [("beta", "Trae Beta")]
        else:
            models = [("alpha", "Codex Alpha"), ("gamma", "Codex Gamma")]
        emit(id=request["id"], result={
            "data": [{"model": model_id, "displayName": label} for model_id, label in models],
            "nextCursor": None,
        })
    elif method == "thread/start":
        assert model is None, "each CLI process should start exactly one thread"
        params = request["params"]
        assert params["sandbox"] == "read-only"
        assert params["approvalPolicy"] == "never"
        assert params["ephemeral"]
        assert params["cwd"] == os.getcwd()
        model = params["model"]
        if lock_directory := os.environ.get("AGENT_PROCESS_LOCK_DIR"):
            hold_process_lock(Path(lock_directory) / model)
        sandbox = "workspaceWrite" if model == "unsafe" else "readOnly"
        emit(id=request["id"], result={
            "thread": {"id": "test-thread"}, "model": model, "approvalPolicy": "never",
            "sandbox": {"type": sandbox},
        })
    elif method == "turn/start":
        params = request["params"]
        assert params["threadId"] == "test-thread"
        assert params["approvalPolicy"] == "never"
        assert params["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
        inputs = params["input"]
        texts = [item["text"] for item in inputs if item["type"] == "text"]
        assert len(texts) <= 1
        prompt = texts[0] if texts else ""
        paths = [Path(item["path"]) for item in inputs if item["type"] == "localImage"]
        assert [path.read_bytes() for path in paths] == [base64.b64decode(data)
                                                         for data in (expected_images if not turns else [])]
        resume_prompt = os.environ.get("EXPECTED_RESUME")
        recovery_prompt = (os.environ.get("EXPECTED_RECOVERY") if "\n\n本轮问题：\n" in prompt else None)
        retry = not turns and model == os.environ.get("RETRY_MODEL") and "\n\n本轮问题：\n" in prompt
        if retry:
            history, question = prompt.rsplit("\n\n本轮问题：\n", 1)
            assert question == os.environ["EXPECTED_FOLLOW_UP"]
            records = [json.loads(line) for line in history.split("\n\n", 1)[1].splitlines()]
            assert records == [{
                "用户": os.environ["EXPECTED_PROMPT"], "回复": f"delta-{model}-done", "状态": "完成",
            }]
            prefix = f"retry-{model}"
        elif not turns and (resume_prompt or recovery_prompt):
            history, question = prompt.rsplit("\n\n本轮问题：\n", 1)
            assert question == (resume_prompt or recovery_prompt)
            records = [json.loads(line) for line in history.split("\n\n", 1)[1].splitlines()]
            first = {
                "用户": os.environ["EXPECTED_PROMPT"], "回复": f"delta-{model}-done", "状态": "完成",
            }
            if image_name := os.environ.get("EXPECTED_HISTORY_IMAGE_NAME"):
                assert "前 1 张图片来自历史第 1 轮" in history
                first["用户"] += f"\n图片：{image_name}"
                first["轮次"] = 1
            assert records[0] == first
            if recovery_prompt:
                assert records[1:] == [{
                    "用户": os.environ["EXPECTED_FOLLOW_UP"], "回复": f"context-{model}", "状态": "已取消",
                }]
            else:
                assert all(record["回复"] in {f"{prefix}-{model}-done" for prefix in ("delta", "context", "resumed")}
                           for record in records)
            prefix = f"resumed-{model}"
        else:
            assert prompt == os.environ["EXPECTED_PROMPT" if turns == 0 else "EXPECTED_FOLLOW_UP"]
            prefix = f"delta-{model}" if turns == 0 else f"context-{model}"
        turn_id = f"test-turn-{turns}"
        item_id = f"test-message-{turns}"
        if turns:
            notice("item/agentMessage/delta", turnId="test-turn-0", itemId="unrelated", delta="late-previous-turn")
        if os.environ.get("EMIT_ACTIVITIES"):
            thought = {"type": "reasoning", "id": "reasoning", "summary": [], "content": ["PRIVATE_REASONING"]}
            notice("item/started", turnId=turn_id, item=thought)
            notice("item/reasoning/summaryTextDelta", turnId=turn_id, itemId="reasoning",
                   summaryIndex=0, delta="Inspecting activity.txt")
            notice("item/completed", turnId=turn_id, item={**thought, "summary": ["Inspecting activity.txt"]})
            tool = {"type": "commandExecution", "id": "command", "status": "inProgress", "command": "cat activity.txt"}
            notice("item/started", turnId=turn_id, item=tool)
        notice("item/agentMessage/delta", turnId=turn_id, itemId=item_id, delta=prefix)
        emit(id=request["id"], result={"turn": {"id": turn_id}})
        release = os.environ.get("RELEASE_FILE")
        if retry:
            release = os.environ.get("RETRY_RELEASE_FILE")
        elif turns == 1 and model == os.environ.get("BLOCK_FOLLOW_UP_MODEL"):
            release = os.environ["FOLLOW_UP_RELEASE_FILE"]
        if release:
            deadline = time.monotonic() + 10
            while not Path(release).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert Path(release).exists(), "streamed output did not arrive before completion"
        if os.environ.get("EMIT_ACTIVITIES"):
            notice("item/completed", turnId=turn_id, item={**tool, "status": "completed", "exitCode": 0})
        notice("item/agentMessage/delta", turnId=turn_id, itemId=item_id, delta="-done")
        notice("item/completed", turnId=turn_id, item={"type": "agentMessage", "id": item_id, "text": f"{prefix}-done"})
        notice("turn/completed", turn={"id": turn_id, "status": "completed"})
        turns += 1

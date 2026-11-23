"""Conversation sessions backed by the installed agent CLIs."""

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from .attachments import IMAGE_SUFFIXES, MAX_IMAGES, MAX_IMAGE_TOTAL, MESSAGE_LIMIT
from .localization import current_language, label, tr

DOUBAO_CONTROL_DIR = Path("/private/tmp") / f"dcli-{os.getuid()}"
DOUBAO_TEXT_ONLY = ("当前 Doubao CLI 的单次任务接口仅支持文本",
                    "The installed Doubao CLI accepts text only for single-turn requests")


@dataclass(frozen=True)
class Activity:
    name: str
    detail: str = ""
    state: str = ""

    @property
    def line(self):
        return self.line_in(current_language())

    def line_in(self, language):
        return " · ".join(part for part in (label(self.state, language), label(self.name, language), self.detail) if part)


@dataclass
class Pane:
    name: str
    model: str
    status: str = "等待启动"
    text: str = ""
    error: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    phase: str = ""
    timeline: dict = field(default_factory=dict)
    elapsed: Optional[str] = None

    def entries(self):
        return self.timeline.values() if self.timeline else ([self.text] if self.text else [])

    @property
    def running(self):
        return self.status in ("等待启动", "运行中")

    @property
    def duration(self):
        if self.elapsed is not None:
            return self.elapsed
        if self.started_at is None:
            return ""
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return f"{end - self.started_at:.1f}s"


class TurnError(ValueError):
    """The provider finished a turn with an error and can accept another one."""


class Messages:
    def __init__(self, pane):
        self.pane = pane

    def update(self, key, text, complete=False):
        if isinstance(text, str) and text:
            key = ("reply", key)
            entries = self.pane.timeline
            entries[key] = text if complete else entries.get(key, "") + text
            self.pane.text = "\n\n".join(value for value in entries.values() if isinstance(value, str))
            self.pane.phase = "正在回复…"

    def summary(self, key, text, complete=False):
        if isinstance(text, str) and text:
            key = ("summary", key)
            previous = self.pane.timeline.get(key)
            detail = text if complete or previous is None else previous.detail + text
            self.pane.timeline[key] = Activity("思考摘要", detail)
        self.pane.phase = "思考中…"

    def tool(self, key, name="", detail="", state="执行中"):
        if not key:
            return
        key = ("tool", key)
        previous = self.pane.timeline.get(key)
        if previous:
            name = name or previous.name
            detail = detail or previous.detail
            if state == "执行中":
                state = previous.state
        if not name:
            return
        detail = " ".join(detail.split()) if isinstance(detail, str) else ""
        if len(detail) > 140:
            detail = detail[:139] + "…"
        self.pane.timeline[key] = Activity(name, detail, state)
        self.pane.phase = "思考中…"


def handle_app_item(messages, item, finished):
    kind = item.get("type")
    if kind == "agentMessage":
        if finished:
            messages.update(item["id"], item.get("text"), complete=True)
        return
    if kind == "reasoning":
        if not finished:
            messages.pane.phase = "思考中…"
        for index, summary in enumerate(item.get("summary") or []):
            messages.summary((item["id"], index), summary, complete=True)
        return

    if kind == "commandExecution":
        name, detail = "命令", item.get("command") or item.get("description")
    elif kind in ("mcpToolCall", "dynamicToolCall"):
        name = "工具 " + " / ".join(part for part in (item.get("server") or item.get("namespace"),
                                                       item.get("tool")) if part)
        detail = ""
    elif kind == "webSearch":
        name, detail = "网页搜索", item.get("query")
    elif kind == "imageView":
        name, detail = "查看图片", item.get("path")
    elif kind == "fileChange":
        name, detail = "文件变更", ""
    else:
        return

    if not finished:
        state = "执行中"
    elif item.get("status") == "declined":
        state = "已拒绝"
    elif item.get("status") == "failed" or item.get("exitCode") not in (None, 0) or item.get("success") is False:
        state = "失败"
    else:
        state = "已完成"
    messages.tool(item.get("id"), name, detail, state)


def handle_tool_block(messages, block):
    kind = block.get("type")
    if kind in ("tool_use", "server_tool_use"):
        arguments = block.get("input")
        if not isinstance(arguments, dict):
            arguments = {}
        detail = next((arguments[key] for key in ("command", "file_path", "path", "pattern", "query", "description")
                       if isinstance(arguments.get(key), str) and arguments[key]), "")
        name = block.get("name")
        messages.tool(block.get("id"), f"工具 {name}" if name else "工具", detail)
    elif kind == "tool_result" or (isinstance(kind, str) and kind.endswith("_tool_result")):
        content = block.get("content")
        failed = block.get("is_error") or (isinstance(content, dict) and str(content.get("type")).endswith("_error"))
        messages.tool(block.get("tool_use_id"), state="失败" if failed else "已完成")
    elif kind in ("thinking", "redacted_thinking"):
        messages.pane.phase = "思考中…"


def find_executable(agent):
    name = agent.executable or agent.cli
    executable = shutil.which(os.path.expanduser(name))
    if not executable:
        raise ValueError(tr(
            f"找不到 {name}，请安装 CLI 或用 --executable 指定路径",
            f"Could not find {name}. Install the CLI or provide its path with --executable.",
        ))
    return os.path.abspath(executable)


def command(agent, cwd, resume=None):
    executable = find_executable(agent)
    model = ["--model=" + agent.model] if agent.model else []

    if agent.cli in ("codex", "traex"):
        return [
            executable, "app-server", "--listen", "stdio://",
            "-c", "mcp_servers={}", "--disable", "hooks", "--disable", "plugins",
            "--disable", "apps", "--disable", "multi_agent", "--disable", "multi_agent_v2",
        ]
    if agent.cli == "claude":
        return [
            executable, "--print", "--verbose", "--output-format", "stream-json",
            "--include-partial-messages", "--no-session-persistence", "--no-chrome",
            "--restricted", "--safe-mode", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}', "--tools", "Read,Glob,Grep",
            "--permission-mode", "dontAsk", "--permission-prompts", "none", *model,
        ]

    sandbox = "/usr/bin/sandbox-exec"
    if sys.platform != "darwin" or not os.access(sandbox, os.X_OK):
        raise ValueError(tr(
            "Doubao 需要 macOS sandbox-exec 才能在只读模式运行",
            "Doubao requires macOS sandbox-exec to run in read-only mode",
        ))
    home = Path.home()
    profile = """
(version 1)
(allow default)
(deny file-write*)
(allow file-write* (literal "/dev/null"))
(allow file-write* (subpath (param "TEMP")))
(allow file-write* (subpath (param "APP_STATE")))
(allow file-write* (subpath (param "USER_STATE")))
(allow file-write* (subpath (param "CONTROL_DIR")))
(allow file-write-create
  (require-all (vnode-type DIRECTORY)
    (require-any (literal (param "CHAT_HOME")) (subpath (param "CHAT_DIR")))))
"""
    workspace = []
    # The root workspace also contains Doubao's state and temporary paths.
    if cwd != Path("/"):
        profile += '(deny file-write* (subpath (param "WORKSPACE")))\n'
        workspace = ["-D", f"WORKSPACE={cwd}"]
    cli = [executable]
    if resume:
        cli.extend(["--resume", resume])
    cli.extend(["-p", "-", "--permission-mode", "ask_always", "--output-format", "stream-json"])
    if agent.model:
        cli.extend(["--model", agent.model])
    return [
        sandbox, "-p", profile,
        "-D", f"TEMP={Path(tempfile.gettempdir()).resolve()}",
        "-D", f"APP_STATE={(home / 'Library/Application Support/doubao_cli').resolve()}",
        "-D", f"USER_STATE={(home / '.doubao').resolve()}",
        "-D", f"CONTROL_DIR={DOUBAO_CONTROL_DIR}",
        "-D", f"CHAT_HOME={(home / 'Doubao').resolve()}",
        "-D", f"CHAT_DIR={(home / 'Doubao/chats').resolve()}",
        *workspace, *cli,
    ]


async def capture_errors(stream, recent):
    while True:
        line = await stream.readline()
        if not line:
            return
        line = line.decode("utf-8", errors="replace").strip()
        if line and not line.startswith("[cli-chat]"):
            recent.append(line)


async def events(stream):
    while True:
        line = await stream.readline()
        if not line:
            return
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


async def send_json(stream, message):
    stream.write(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
    await stream.drain()


class AppSession:
    def __init__(self, process):
        self.input = process.stdin
        self.output = events(process.stdout)
        self.pane = None
        self.messages = None
        self.model = None
        self.thread_id = None
        self.turn_id = None
        self.pending_turn_events = []
        self.turn = None
        self.request_id = 0

    async def send(self, message):
        await send_json(self.input, message)

    async def handle(self, event):
        method = event.get("method")
        if method and "id" in event:
            await self.send({
                "id": event["id"],
                "error": {"code": -32601, "message": "This client does not grant permissions."},
            })
            return

        params = event.get("params") or {}
        if self.pane is None or params.get("threadId") != self.thread_id:
            return
        if method not in (
            "item/agentMessage/delta", "item/started", "item/completed",
            "item/reasoning/summaryTextDelta", "turn/completed", "model/rerouted",
        ):
            return
        if self.turn_id is None:
            self.pending_turn_events.append(event)
            return
        event_turn = params["turn"]["id"] if method == "turn/completed" else params.get("turnId")
        if event_turn != self.turn_id:
            return
        if method == "item/agentMessage/delta":
            self.messages.update(params["itemId"], params["delta"])
        elif method == "item/reasoning/summaryTextDelta":
            self.messages.summary((params["itemId"], params.get("summaryIndex", 0)), params.get("delta"))
        elif method in ("item/started", "item/completed"):
            handle_app_item(self.messages, params["item"], finished=method == "item/completed")
        elif method == "turn/completed":
            self.turn = params["turn"]
        elif method == "model/rerouted" and params.get("toModel"):
            self.model = params["toModel"]
            self.pane.model = self.model

    async def request(self, method, params):
        self.request_id += 1
        await self.send({"id": self.request_id, "method": method, "params": params})
        async for event in self.output:
            if event.get("id") == self.request_id and "method" not in event:
                if "error" in event:
                    raise ValueError(event["error"].get("message") or tr(f"{method} 失败", f"{method} failed"))
                return event["result"]
            await self.handle(event)
        raise ValueError(tr(f"CLI 在 {method} 完成前退出", f"The CLI exited before {method} completed"))

    async def initialize(self):
        await self.request("initialize", {
            "clientInfo": {"name": "ask-agents", "title": "ask-agents", "version": "0.1.0"},
            "capabilities": None,
        })
        await self.send({"method": "initialized"})

    async def start(self, agent, cwd):
        await self.initialize()
        thread = await self.request("thread/start", {
            "model": agent.model,
            "cwd": str(cwd),
            "ephemeral": True,
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "developerInstructions": "Inspect the workspace without changing files or external state.",
        })
        if (thread.get("sandbox") or {}).get("type") != "readOnly" or thread.get("approvalPolicy") != "never":
            raise ValueError(tr(
                "CLI 未确认只读模式和禁止提升权限，已停止",
                "The CLI stopped because it did not confirm read-only mode with escalations disabled",
            ))
        self.thread_id = thread["thread"]["id"]
        self.model = thread.get("model")

    async def ask(self, pane, prompt, image_paths):
        self.pane = pane
        pane.model = self.model or pane.model
        self.messages = Messages(pane)
        self.turn_id = None
        self.turn = None
        self.pending_turn_events.clear()

        started = await self.request("turn/start", {
            "threadId": self.thread_id,
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
            "input": [
                *([{"type": "text", "text": prompt, "text_elements": []}] if prompt.strip() else []),
                *({"type": "localImage", "path": str(path)} for path in image_paths),
            ],
        })
        self.turn_id = started["turn"]["id"]
        for event in self.pending_turn_events:
            await self.handle(event)
        self.pending_turn_events.clear()
        while self.turn is None:
            try:
                await self.handle(await self.output.__anext__())
            except StopAsyncIteration:
                raise ValueError(tr("CLI 在回答完成前退出", "The CLI exited before finishing its reply")) from None
        if self.turn.get("status") != "completed":
            raise TurnError((self.turn.get("error") or {}).get("message") or tr("回答中断", "Reply interrupted"))


async def read_print_reply(process, pane):
    messages = Messages(pane)
    message_id = "message"
    session_id = None
    async for record in events(process.stdout):
        if record.get("parent_tool_use_id"):
            continue
        kind = record.get("type")
        if kind == "system" and record.get("subtype") == "init":
            session_id = record.get("session_id") or session_id
            pane.model = record.get("model") or pane.model
        elif kind == "stream_event":
            event = record["event"]
            if event.get("type") == "message_start":
                message_id = event["message"].get("id", message_id)
            elif event.get("type") == "content_block_start":
                handle_tool_block(messages, event.get("content_block") or {})
            elif event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "text_delta":
                    messages.update((record.get("message_id", message_id), event.get("index", 0)), delta.get("text"))
                elif delta.get("type") == "thinking_delta":
                    pane.phase = "思考中…"
        elif kind in ("assistant", "user"):
            message = record.get("message") or {}
            content = message.get("content")
            for index, block in enumerate(content if isinstance(content, list) else []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and kind == "assistant":
                    messages.update((message.get("id") or message_id, index), block.get("text"), complete=True)
                else:
                    handle_tool_block(messages, block)
        elif kind == "result":
            if record.get("is_error"):
                errors = record.get("errors") or []
                details = "; ".join(map(str, errors)) or record.get("result")
                raise TurnError(details or record.get("subtype") or tr("CLI 返回错误", "The CLI returned an error"))
            if not pane.text:
                result = record.get("result")
                messages.update("result", result.get("content") if isinstance(result, dict) else result)
            return record.get("session_id") or session_id
    code = await process.wait()
    raise ValueError(tr(f"CLI 在回答完成前退出（退出码 {code}）", f"The CLI exited before finishing its reply (exit code {code})"))


async def stop(process):
    # Stop the whole group, including shell commands or a CLI launcher.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            if process.returncode is not None:
                break
            process.send_signal(sig)
        if sig == signal.SIGTERM and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=0.8)
            except asyncio.TimeoutError:
                pass
    await process.wait()


async def start_process(*argv, **options):
    started = asyncio.create_task(asyncio.create_subprocess_exec(
        *argv, start_new_session=True, limit=MESSAGE_LIMIT, **options,
    ))
    try:
        return await asyncio.shield(started)
    except asyncio.CancelledError:
        # Keep the process handle if cancellation arrives during startup.
        process, = await asyncio.gather(started, return_exceptions=True)
        if isinstance(process, asyncio.subprocess.Process):
            await stop(process)
        raise


async def finish_turn(tasks, panes):
    for task, pane in zip(tasks, panes):
        if not task.done() and pane.running:
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for pane in panes:
        if pane.status == "等待启动":
            pane.status = "已取消"


class AgentConversation:
    def __init__(self, agent, cwd, context="", context_images=None):
        self.agent = agent
        self.cwd = cwd
        self.context = context
        self.context_images = context_images
        self.process = None
        self.stderr_task = None
        self.recent = deque(maxlen=6)
        self.app = None
        self.session_id = None
        self.model = agent.model or "默认模型"
        self.has_answer = False

    @property
    def can_continue(self):
        if self.agent.cli == "doubao":
            return self.session_id is not None
        return self.process is not None and self.process.returncode is None

    async def connect(self, prompt):
        if self.process:
            if self.process.returncode is None:
                return
            await self.close()

        doubao = self.agent.cli == "doubao"
        if self.has_answer and (not doubao or not self.session_id):
            raise ValueError(tr("原会话已断开，无法继续其上下文", "The original session disconnected and its context is unavailable"))

        argv = command(self.agent, self.cwd, resume=self.session_id if doubao else None)
        if self.agent.cli == "claude":
            argv += ["--input-format", "stream-json"]
        with (tempfile.TemporaryFile() if doubao else nullcontext()) as input_file:
            if input_file is not None:
                input_file.write(prompt.encode("utf-8"))
                input_file.seek(0)
            self.process = await start_process(
                *argv, cwd=self.cwd, stdin=input_file if doubao else asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "NO_COLOR": "1"},
            )
        self.stderr_task = asyncio.create_task(capture_errors(self.process.stderr, self.recent))
        if self.agent.cli in ("codex", "traex"):
            self.app = AppSession(self.process)
            await self.app.start(self.agent, self.cwd)

    async def ask(self, pane, prompt, images):
        if self.context:
            image_note = ""
            if self.context_images and self.agent.cli != "doubao":
                number, earlier_images = self.context_images
                combined = earlier_images + images
                if len(combined) <= MAX_IMAGES and sum(len(image.data) for image in combined) <= MAX_IMAGE_TOTAL:
                    images = combined
                    image_note = f"随附的前 {len(earlier_images)} 张图片来自历史第 {number} 轮，之后的图片属于本轮。\n"
            prompt = ("下面是本会话中用户与你的历史问答，每行是一条 JSON 记录。"
                      f"请参考这些记录回答最后的本轮问题。\n{image_note}\n"
                      f"{self.context}\n\n本轮问题：\n{prompt}")
        await self.connect(prompt)
        pane.model = self.model
        if self.app:
            with (tempfile.TemporaryDirectory(prefix="ask-agents-images-") if images else nullcontext()) as directory:
                paths = []
                for index, image in enumerate(images):
                    path = Path(directory) / f"{index}{IMAGE_SUFFIXES[image.media_type]}"
                    path.write_bytes(image.data)
                    paths.append(path)
                await self.app.ask(pane, prompt, paths)
        else:
            if self.agent.cli == "claude":
                content = prompt
                if images:
                    content = [{"type": "text", "text": prompt}] if prompt.strip() else []
                    content.extend({
                        "type": "image",
                        "source": {"type": "base64", "media_type": image.media_type, "data": image.base64()},
                    } for image in images)
                await send_json(self.process.stdin, {
                    "type": "user",
                    "message": {"role": "user", "content": content},
                    "parent_tool_use_id": None,
                    "session_id": self.session_id or "default",
                })
            session_id = await read_print_reply(self.process, pane)
            if self.agent.cli == "doubao":
                code = await self.process.wait()
                if code:
                    raise ValueError(tr(f"Doubao CLI 退出（退出码 {code}）", f"Doubao CLI exited (exit code {code})"))
                if str(session_id).isascii() and str(session_id).isdecimal():
                    self.session_id = str(session_id)
            elif session_id:
                self.session_id = session_id
        self.model = pane.model
        self.has_answer = True
        self.context = ""
        self.context_images = None
        if not pane.text.strip():
            raise TurnError(tr("CLI 没有返回文本", "The CLI returned no text"))

    async def run(self, pane, prompt, timeout, images=()):
        self.recent.clear()
        pane.started_at = time.monotonic()
        pane.status = "运行中"
        pane.phase = "思考中…"
        reuse = False
        try:
            if images and self.agent.cli == "doubao":
                pane.status = "失败"
                pane.error = tr(*DOUBAO_TEXT_ONLY)
                return
            await asyncio.wait_for(self.ask(pane, prompt, images), timeout)
            reuse = True
            pane.status = "完成"
        except asyncio.TimeoutError:
            pane.status = "超时"
            pane.error = tr(f"已等待 {timeout:g} 秒", f"Timed out after {timeout:g} seconds")
        except asyncio.CancelledError:
            pane.status = "已取消"
            raise
        except TurnError as exc:
            reuse = True
            if self.agent.cli != "doubao":
                self.context = ""
                self.context_images = None
            pane.status = "失败"
            pane.error = str(exc)
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
            pane.status = "失败"
            pane.error = str(exc) or type(exc).__name__
        finally:
            pane.finished_at = time.monotonic()
            for key, entry in pane.timeline.items():
                if isinstance(entry, Activity) and entry.state == "执行中":
                    state = "未报告结果" if pane.status == "完成" else "中断"
                    pane.timeline[key] = replace(entry, state=state)
            if not reuse or self.agent.cli == "doubao":
                await self.close()
            if pane.error and self.recent:
                pane.error += "\n" + "\n".join(self.recent)

    async def close(self):
        process, stderr_task = self.process, self.stderr_task
        self.process = self.stderr_task = self.app = None
        if process:
            cleanup = asyncio.create_task(stop(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
        if stderr_task:
            await asyncio.gather(stderr_task, return_exceptions=True)

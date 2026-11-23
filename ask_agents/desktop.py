"""JSON-lines connection between the macOS app and the existing CLI runners."""

import asyncio
import json
import math
import os
import re
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from .attachments import ImageAttachment, MESSAGE_LIMIT, load_images
from .cli import checked_prompt
from .config import CLIS, Agent, config_path, read_config, save_config
from .history import Conversation, Exchange, History
from .localization import label, set_language, tr
from .models import model_choices
from .runners import DOUBAO_TEXT_ONLY, Activity, Pane, finish_turn


def add_desktop_paths():
    home = Path.home()
    nvm = Path(os.environ.get("NVM_DIR") or home / ".nvm")
    versions = [path for path in (nvm / "versions/node").glob("v*.*.*")
                if re.fullmatch(r"v\d+\.\d+\.\d+", path.name)]
    versions.sort(key=lambda path: tuple(map(int, path.name[1:].split("."))), reverse=True)
    try:
        default = (nvm / "alias/default").read_text().strip().removeprefix("v").split(".")
        versions.sort(key=lambda path: path.name[1:].split(".")[:len(default)] != default)
    except OSError:
        pass

    paths = [*os.get_exec_path(), str(home / ".local/bin"), str(home / "Library/pnpm"),
             *(str(path / "bin") for path in versions), "/opt/homebrew/bin", "/usr/local/bin"]
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(path for path in paths if os.path.isabs(path)))


def directory(value):
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError(tr(f"{path} 不是目录", f"{path} is not a directory"))
    return path


def snapshot(pane):
    return {
        "name": pane.name, "model": pane.model, "status": pane.status, "error": pane.error,
        "duration": pane.duration, "phase": pane.phase, "statusEnglish": label(pane.status, "en"),
        "phaseEnglish": label(pane.phase, "en"),
        "entries": [
            {"activity": entry.line_in("zh"), "activityEnglish": entry.line_in("en")}
            if isinstance(entry, Activity) else {"text": entry}
            for entry in pane.entries()
        ],
    }


class Desktop:
    def __init__(self):
        self.path = config_path()
        self.history = History(self.path)
        self.conversation = None
        self.sessions = {}
        self.turn = None
        self.current_exchange = None
        self.turn_tasks = {}
        self.retries = {}
        self.catalogs = set()
        self.output_open = True

    def send(self, request, kind, **fields):
        if self.output_open:
            try:
                print(json.dumps({"id": request["id"], "kind": kind, **fields}, ensure_ascii=True), flush=True)
            except BrokenPipeError:
                self.output_open = False

    def configuration(self):
        return {
            "agents": [asdict(agent) for agent in read_config(self.path)],
            "configPath": str(self.path),
            "installed": [cli for cli in CLIS if shutil.which(cli)],
        }

    async def close_sessions(self):
        sessions, self.sessions = self.sessions, {}
        await asyncio.gather(*(session.close() for session in sessions.values()))

    def running_turns(self):
        return [task for task in (self.turn, *self.retries.values()) if task and not task.done()]

    async def exit_agent(self, request):
        name = request["agentName"]
        conversation = self.conversation
        if conversation is None or not any(agent.name == name for agent in conversation.agents):
            raise ValueError(tr("找不到当前对话里的 agent", "Agent not found in the current conversation"))
        conversation.exited_agents.add(name)
        retry = self.retries.get(name)
        if retry and not retry.done():
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)
        if name in self.turn_tasks:
            task, pane = self.turn_tasks[name]
            if not task.done() and pane.running:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        session = self.sessions.pop(name, None)
        if session:
            await session.close()
        warning = None
        try:
            self.history.save_exits(conversation)
        except (OSError, ValueError) as exc:
            warning = tr(f"退出状态未保存到历史会话。{exc}", f"Could not save the agent’s exit to history. {exc}")
        self.send(request, "result", historyError=warning)

    def timeout(self, request):
        timeout = request["timeout"]
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(tr("超时须为正数", "Timeout must be positive"))
        return timeout

    async def send_turn(self, request):
        agents = tuple(agent for agent in (Agent(**item) for item in request["agents"]) if agent.enabled)
        if not agents or len({agent.name for agent in agents}) != len(agents):
            raise ValueError(tr(
                "请启用至少一个 agent，并为每个实例使用不同的名称",
                "Enable at least one agent and use a unique name for each agent",
            ))
        cwd = directory(request["cwd"])
        images = load_images(request.get("images", []), ImageAttachment.from_wire)
        prompt = checked_prompt(request.get("prompt", ""), images)
        timeout = self.timeout(request)

        if self.conversation is None or (agents, cwd) != (self.conversation.agents, self.conversation.cwd):
            await self.close_sessions()
            self.conversation = Conversation(cwd, agents)
        conversation = self.conversation
        active_agents = conversation.active_agents
        if not active_agents:
            raise ValueError(tr(
                "这段对话的 agent 已全部退出，请开启新对话",
                "All agents have left. Start a new conversation to continue.",
            ))
        await conversation.prepare_sessions(self.sessions)

        panes = [Pane(agent.name, agent.model or "默认模型") for agent in active_agents]
        exchange = self.current_exchange = Exchange(prompt, panes, images)
        self.turn_tasks = {
            pane.name: (asyncio.create_task(self.sessions[pane.name].run(pane, prompt, timeout, images)), pane)
            for pane in panes
        }
        tasks = [task for task, _ in self.turn_tasks.values()]
        try:
            kind = await self.stream_panes(request, conversation, tasks, panes)
        finally:
            self.current_exchange = None
            self.turn_tasks = {}

        try:
            item = self.history.record(conversation, exchange)
        except (OSError, ValueError) as exc:
            self.send(request, kind, historyError=tr(
                f"本轮未保存到历史会话。{exc}",
                f"Could not save this turn to history. {exc}",
            ))
        else:
            self.send(request, kind, historyItem=asdict(item))

    async def retry_agent(self, request):
        name = request["agentName"]
        conversation = self.conversation
        agent = next((agent for agent in conversation.active_agents if agent.name == name), None) if conversation else None
        if agent is None:
            raise ValueError(tr("找不到当前对话里可重试的 agent", "Agent not available for retry in the current conversation"))
        current = self.current_exchange
        exchange = current or (conversation.exchanges[-1] if conversation.exchanges else None)
        previous = exchange.reply(name) if exchange else None
        if previous is None or previous.running:
            raise ValueError(tr("请在这个 agent 的回复结束后重试", "Wait for this agent to finish before retrying"))
        if exchange.images and agent.cli == "doubao":
            raise ValueError(tr(*DOUBAO_TEXT_ONLY))
        directory(str(conversation.cwd))
        timeout = self.timeout(request)
        session = conversation.session(agent, before=None if current else -1)
        pane = Pane(name, agent.model or "默认模型")
        previous_work = self.turn_tasks.get(name)

        async def rerun():
            if previous_work:
                task, _ = previous_work
                await asyncio.gather(asyncio.shield(task), return_exceptions=True)
            old = self.sessions.pop(name, None)
            if old:
                await old.close()
            self.sessions[name] = session
            try:
                await session.run(pane, exchange.prompt, timeout, exchange.images)
            finally:
                if pane.status != "完成":
                    self.sessions.pop(name, None)
                    await session.close()

        attempt = asyncio.create_task(rerun())
        kind = await self.stream_panes(request, conversation, [attempt], [pane])
        if kind == "cancelled" and not pane.text:
            self.send(request, "panes", panes=[snapshot(previous)])
            self.send(request, kind)
            return
        if self.current_exchange is exchange:
            exchange.replace_reply(pane)
            self.send(request, kind)
        elif conversation.exchanges and conversation.exchanges[-1] is exchange:
            try:
                item = self.history.replace_latest_reply(conversation, pane)
            except (OSError, ValueError) as exc:
                if exchange.reply(name) is previous:
                    self.sessions.pop(name, None)
                    await session.close()
                    self.send(request, "panes", panes=[snapshot(previous)])
                self.send(request, kind, historyError=tr(
                    f"重试结果未保存到历史会话。{exc}",
                    f"Could not save the retried reply to history. {exc}",
                ))
            else:
                self.send(request, kind, historyItem=asdict(item))
        else:
            self.send(request, "error", message=tr("找不到要更新的回复", "Could not find the reply to update"))

    async def stream_panes(self, request, conversation, tasks, panes):
        latest = None
        kind = "result"
        try:
            pending = set(tasks)
            while pending:
                _, pending = await asyncio.wait(pending, timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
                current = [snapshot(pane) for pane in panes]
                if current != latest:
                    self.send(request, "panes", panes=current)
                    latest = current
            for task in tasks:
                if not task.cancelled():
                    task.result()
        except asyncio.CancelledError:
            kind = "cancelled"
        finally:
            await finish_turn(tasks, panes)
            for pane in panes:
                if pane.name in conversation.exited_agents and pane.status == "已取消":
                    pane.status = "已退出"
            current = [snapshot(pane) for pane in panes]
            if current != latest:
                self.send(request, "panes", panes=current)
        return kind

    async def reply(self, request):
        try:
            action = request["action"]
            if action == "load":
                self.send(request, "result", **self.configuration())
            elif action == "history":
                self.send(request, "result", history=[asdict(item) for item in self.history.list()])
            elif action == "openHistory":
                if self.running_turns():
                    raise ValueError(tr(
                        "请在本轮结束后切换会话",
                        "Wait for the current turn to finish before switching conversations",
                    ))
                conversation = self.history.open(request["conversationID"])
                await self.close_sessions()
                self.conversation = conversation
                exchanges = [{
                    "prompt": exchange.prompt,
                    "images": [image.wire() for image in exchange.images],
                    "panes": [snapshot(pane) for pane in exchange.panes],
                } for exchange in conversation.exchanges]
                self.send(request, "result", agents=[asdict(agent) for agent in conversation.agents],
                          cwd=str(conversation.cwd), exchanges=exchanges, exitedAgents=sorted(conversation.exited_agents))
            elif action == "save":
                original = [Agent(**item) for item in request["original"]]
                updated = [Agent(**item) for item in request["agents"]]
                if len({agent.name for agent in updated}) != len(updated):
                    raise ValueError(tr("已存在同名实例", "An agent with that name already exists"))
                save_config(self.path, original, updated)
                self.send(request, "result", **self.configuration())
            elif action == "models":
                agent = Agent(**request["agent"])
                cwd = directory(request["cwd"]) if request.get("cwd") else Path.home()
                models = await model_choices(agent, cwd)
                self.send(request, "result", models=[{"id": key, "label": value} for key, value in models.items()])
            elif action == "ask":
                await self.send_turn(request)
            elif action == "retryAgent":
                await self.retry_agent(request)
            elif action == "exitAgent":
                await self.exit_agent(request)
            elif action == "reset":
                if self.running_turns():
                    raise ValueError(tr(
                        "请在本轮结束后开启新对话",
                        "Wait for the current turn to finish before starting a new conversation",
                    ))
                configuration = self.configuration()
                name = request.get("agentName")
                if name is not None and not any(agent["name"] == name for agent in configuration["agents"]):
                    self.send(request, "error", message=tr(
                        "该 Agent 已移除",
                        "This agent has been removed",
                    ), **configuration)
                    return
                await self.close_sessions()
                self.conversation = None
                self.send(request, "result", **configuration)
            else:
                raise ValueError(tr(f"未知操作 {action}", f"Unknown action {action}"))
        except asyncio.CancelledError:
            self.send(request, "cancelled")
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            self.send(request, "error", message=str(exc) or type(exc).__name__)

    async def run(self):
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=MESSAGE_LIMIT)
        transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        try:
            while line := await reader.readline():
                request = json.loads(line)
                # Tasks spawned for a turn retain the language of that request.
                set_language(request.get("language"))
                action = request["action"]
                if action == "ask":
                    if self.running_turns():
                        self.send(request, "error", message=tr("上一轮还在运行", "The previous turn is still running"))
                    else:
                        self.turn = asyncio.create_task(self.reply(request))
                        await asyncio.sleep(0)
                elif action == "retryAgent":
                    name = request.get("agentName")
                    if not isinstance(name, str):
                        self.send(request, "error", message=tr("请指定要重试的 agent", "Specify which agent to retry"))
                    elif name in self.retries and not self.retries[name].done():
                        self.send(request, "error", message=tr("这个 agent 正在重试", "This agent is already retrying"))
                    else:
                        task = asyncio.create_task(self.reply(request))
                        self.retries[name] = task
                        task.add_done_callback(lambda finished, name=name: self.forget_retry(name, finished))
                        await asyncio.sleep(0)
                elif action == "cancel":
                    running = self.running_turns()
                    for task in running:
                        task.cancel()
                    await asyncio.gather(*running, return_exceptions=True)
                elif action == "models":
                    task = asyncio.create_task(self.reply(request))
                    self.catalogs.add(task)
                    task.add_done_callback(self.catalogs.discard)
                else:
                    await self.reply(request)
        finally:
            transport.close()
            tasks = [*self.catalogs, *self.running_turns()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.close_sessions()

    def forget_retry(self, name, task):
        if self.retries.get(name) is task:
            del self.retries[name]


if __name__ == "__main__":
    add_desktop_paths()
    asyncio.run(Desktop().run())

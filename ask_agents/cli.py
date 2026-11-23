"""Entry point for ask-agents."""

import argparse
import asyncio
import curses
import math
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from .attachments import ImageAttachment, load_images
from .config import CLIS, Agent, config_path, edit_config, read_config, save_config
from .history import Conversation, Exchange, History
from .localization import label, set_language, terminal_language, tr
from .runners import Activity, Pane, finish_turn
from .setup import configure
from .terminal import Dashboard, LineOutput, TerminalHangup, clean, run_with_hangup_handler, set_terminal_title

MAX_PROMPT = 200_000


def parser():
    command = argparse.ArgumentParser(
        description=tr("使用保存的配置，并行向多个 agent 提问。", "Ask several agents in parallel using your saved settings.")
    )
    command.add_argument("--config", type=Path, default=config_path(), metavar="FILE", help=tr(
        "配置文件路径",
        "Path to the configuration file",
    ))
    command.add_argument("--language", choices=("zh", "en"), help=tr("界面语言，默认跟随终端区域设置",
                                                                   "Interface language. Defaults to the terminal locale."))
    command.add_argument("-C", "--cwd", type=Path, metavar="DIR", help=tr("指定只读工作目录", "Read-only working directory"))
    command.add_argument("-p", "--prompt", metavar="TEXT", help=tr("直接发送文本问题", "Send a text prompt directly"))
    command.add_argument("-i", "--image", action="append", default=[], type=Path, metavar="FILE",
                         help=tr("为首轮添加图片，可重复指定", "Attach an image to the first turn. Repeat for more images."))
    command.add_argument("--plain", action="store_true", help=tr(
        "每个 agent 完成时输出带名称的纯文本",
        "Print each named agent’s reply as plain text when it finishes",
    ))
    command.add_argument("--resume", metavar="ID", help=tr(
        "继续历史会话，支持输入 ID 前缀",
        "Resume a conversation by ID or unique prefix",
    ))
    command.add_argument("--timeout", type=float, default=600, metavar="SECONDS", help=tr(
        "每轮单个 agent 的最长运行秒数（默认 600）",
        "Maximum seconds per turn for each agent (default 600)",
    ))
    actions = command.add_subparsers(dest="action")
    actions.add_parser("config", help=tr("打开交互界面修改已保存的 agent 和模型", "Open interactive agent and model settings"))
    add = actions.add_parser("add", help=tr("按名称新增或更新一个 agent 配置", "Add or update an agent by name"))
    add.add_argument("name")
    add.add_argument("cli", choices=CLIS)
    add.add_argument("-m", "--model", help=tr("模型 ID，省略时使用 CLI 默认模型", "Model ID. Uses the CLI default when omitted."))
    add.add_argument("--executable", help=tr("CLI 的路径或命令名", "Path or command name for the CLI"))
    actions.add_parser("list", help=tr("列出当前配置", "List configured agents"))
    history = actions.add_parser("history", help=tr("列出历史会话或查看指定会话", "List conversations or view a conversation"))
    history.add_argument("id", nargs="?", help=tr("会话 ID 或可唯一匹配的前缀", "Conversation ID or unique prefix"))
    remove = actions.add_parser("remove", help=tr("移除指定名称的配置", "Remove an agent by name"))
    remove.add_argument("name")
    return command


def checked_prompt(prompt, images=()):
    if not prompt.strip() and not images:
        raise ValueError(tr("请输入问题或添加图片", "Enter a question or attach an image"))
    if len(prompt) > MAX_PROMPT:
        raise ValueError(tr(f"问题最多支持 {MAX_PROMPT:,} 个字符", f"Prompts can contain up to {MAX_PROMPT:,} characters"))
    return prompt


@asynccontextmanager
async def open_sessions():
    sessions = {}
    try:
        yield sessions
    finally:
        await asyncio.gather(*(session.close() for session in sessions.values()))


async def run_all(sessions, panes, prompt, timeout, output, history, conversation, images=()):
    await conversation.prepare_sessions(sessions)
    tasks = [asyncio.create_task(sessions[pane.name].run(pane, prompt, timeout, images)) for pane in panes]
    try:
        while not all(task.done() for task in tasks):
            if not output.refresh():
                return 1
            await asyncio.sleep(0.05)
        await asyncio.gather(*tasks)
        output.refresh()
        return int(any(pane.status != "完成" for pane in panes))
    finally:
        await finish_turn(tasks, panes)
        try:
            history.record(conversation, Exchange(prompt, panes, images))
        except (OSError, ValueError) as exc:
            output.history_error(tr(f"本轮未保存到历史会话。{exc}", f"Could not save this turn to history. {exc}"))


async def run_once(conversation, prompt, timeout, history, images=()):
    panes = [Pane(agent.name, agent.model or "默认模型") for agent in conversation.active_agents]
    async with open_sessions() as sessions:
        return await run_all(sessions, panes, prompt, timeout, LineOutput(panes), history, conversation, images)


async def run_conversation(screen, conversation, prompt, timeout, title, history, images=()):
    exchanges = conversation.exchanges
    latest = exchanges[-1] if exchanges else None
    panes = [(latest.reply(agent.name) if latest else None)
             or Pane(agent.name, agent.model or "默认模型", status="就绪")
             for agent in conversation.active_agents]
    dashboard = Dashboard(screen, title, conversation.cwd, panes, exchanges)
    result = 0
    async with open_sessions() as sessions:
        if prompt is None:
            prompt = await dashboard.wait_for_prompt(checked_prompt)
        while prompt is not None:
            panes = dashboard.start_reply(prompt, images)
            set_terminal_title(title, dashboard.exchanges[-1].display_prompt)
            round_status = await run_all(sessions, panes, prompt, timeout, dashboard, history, conversation, images)
            images = ()
            result = max(result, round_status)
            prompt = await dashboard.wait_for_prompt(checked_prompt)
    return result, dashboard.exchanges


def prepare_terminal(screen):
    curses.raw()
    curses.nonl()
    curses.set_escdelay(35)
    if curses.has_colors():
        try:
            curses.use_default_colors()
        except curses.error:
            pass
    screen.keypad(True)


def terminal_config(screen, agents, cwd, path, title):
    prepare_terminal(screen)
    updated = configure(screen, agents, cwd, title, ask_after=False)
    save_config(path, agents, updated)


def terminal_run(screen, agents, cwd, prompt, timeout, path, title, history, conversation, images=()):
    prepare_terminal(screen)
    if conversation is None:
        updated = configure(screen, agents, cwd, title, ask_after=True)
        save_config(path, agents, updated)
        conversation = Conversation(cwd, tuple(agent for agent in updated if agent.enabled))
    return asyncio.run(run_conversation(screen, conversation, prompt, timeout, title, history, images))


def print_transcript(exchanges, *, error_stream=None):
    if error_stream is None:
        error_stream = sys.stderr
    names = dict.fromkeys(pane.name for exchange in exchanges for pane in exchange.panes)
    for name in names:
        print(f"\n── {name} ──")
        for exchange in exchanges:
            reply = exchange.reply(name)
            if reply is None:
                continue
            print(f"\n› {clean(exchange.display_prompt)}")
            for entry in reply.entries():
                print(clean("↳ " + entry.line if isinstance(entry, Activity) else entry))
            if reply.error:
                print(clean(reply.error), file=error_stream)
            print(f"{clean(label(reply.model))} · {label(reply.status)} · {reply.duration}")


def main():
    language_options = argparse.ArgumentParser(add_help=False)
    language_options.add_argument("--language")
    early, _ = language_options.parse_known_args()
    set_language(early.language or terminal_language())
    command = parser()
    args = command.parse_args()
    if args.resume and args.action:
        command.error(tr("--resume 只能用于继续会话", "--resume is only available when sending or composing a prompt"))
    path = args.config.expanduser()
    try:
        if args.action == "add":
            agent = Agent(args.name, args.cli, args.model, args.executable)
            edit_config(path, agent.name, agent)
            model = agent.model or label("默认模型")
            print(tr(f"已保存 {agent.name} · {agent.cli} · {model}", f"Saved {agent.name} · {agent.cli} · {model}"))
            return 0
        if args.action == "remove":
            edit_config(path, args.name)
            print(tr(f"已移除 {args.name}", f"Removed {args.name}"))
            return 0

        history = History(path)
        if args.action == "history":
            if args.id:
                conversation = history.open(args.id)
                print(f"{conversation.id} · {clean(str(conversation.cwd))}")
                print_transcript(conversation.exchanges, error_stream=sys.stdout)
            else:
                for item in history.list():
                    print(f"{item.id}\t{clean(item.title)}\t{clean(item.cwd)}")
            return 0

        conversation = history.open(args.resume) if args.resume else None
        agents = [] if conversation else read_config(path)
        if args.action == "list":
            print(path)
            for agent in agents:
                status = tr("启用", "Enabled") if agent.enabled else tr("停用", "Disabled")
                print(f"{agent.name}\t{agent.cli}\t{agent.model or label('默认模型')}\t{status}")
            return 0
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError(tr("--timeout 须为正数", "--timeout must be positive"))
        cwd = (args.cwd or (conversation.cwd if conversation else Path.cwd())).expanduser().resolve(strict=True)
        if not cwd.is_dir():
            raise ValueError(tr(f"{cwd} 不是目录", f"{cwd} is not a directory"))
        if conversation and cwd != conversation.cwd:
            raise ValueError(tr(
                f"历史会话使用原工作目录 {conversation.cwd}",
                f"This conversation uses its original working directory {conversation.cwd}",
            ))
        if conversation and not conversation.active_agents:
            raise ValueError(tr(
                "这段对话的 agent 已全部退出，可用 history 回看，或开启新对话",
                "All agents have left. Use history to review the conversation or start a new one.",
            ))

        terminal = sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") not in (None, "dumb")
        if args.action == "config":
            if not terminal:
                raise ValueError(tr(
                    f"请在交互式终端运行 {command.prog} config",
                    f"Run {command.prog} config in an interactive terminal",
                ))
            run_with_hangup_handler(curses.wrapper, terminal_config, agents, cwd, path, command.prog)
            print(tr(f"配置文件 · {path}", f"Config file · {path}"))
            return 0

        prompt = args.prompt
        if prompt is None and not sys.stdin.isatty():
            prompt = sys.stdin.read(MAX_PROMPT + 1)
        images = load_images(args.image, ImageAttachment.from_path)
        if prompt is None and images:
            prompt = ""
        if prompt is not None:
            prompt = checked_prompt(prompt, images)
        elif not terminal or args.plain:
            raise ValueError(tr("请用 --prompt 或标准输入提供问题", "Provide a prompt with --prompt or standard input"))

        enabled = tuple(agent for agent in agents if agent.enabled)
        if conversation is None and enabled:
            conversation = Conversation(cwd, enabled)
        if prompt is not None and conversation is None:
            raise ValueError(tr(
                f"没有已启用的 agent，请在终端运行 {command.prog} config",
                f"No agents are enabled. Run {command.prog} config in a terminal.",
            ))
        if not terminal or args.plain:
            return run_with_hangup_handler(asyncio.run, run_once(conversation, prompt, args.timeout, history, images))

        result, exchanges = run_with_hangup_handler(
            curses.wrapper, terminal_run, agents, cwd, prompt, args.timeout, path, command.prog, history, conversation,
            images,
        )
        if exchanges:
            print_transcript(exchanges)
        return result
    except TerminalHangup:
        return 128 + signal.SIGHUP
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, curses.error) as exc:
        print(f"{command.prog}: {exc}", file=sys.stderr)
        return 1

"""Select and configure several installed CLI instances in one terminal session."""

import asyncio
import curses
import shutil
from dataclasses import dataclass, replace

from .config import CLIS, Agent
from .localization import label as localized_label, tr
from .models import model_choices
from .terminal import cells, cursor, draw, read_key

ENTER = ("\r", curses.KEY_ENTER)
MANUAL = object()
CANCEL = object()


@dataclass
class Row:
    agent: Agent
    configured: bool


def available_name(base, rows):
    names = {row.agent.name for row in rows}
    if base not in names:
        return base
    number = 2
    while True:
        suffix = f"-{number}"
        name = base[:32 - len(suffix)] + suffix
        if name not in names:
            return name
        number += 1


def configure(screen, saved, cwd, title, *, ask_after):
    installed = [cli for cli in CLIS if shutil.which(cli)]
    rows = [Row(agent, True) for agent in saved]
    catalogs = {}

    def show_available():
        existing = {row.agent.cli for row in rows}
        for cli in installed:
            if cli not in existing:
                rows.append(Row(Agent(available_name(cli, rows), cli, enabled=False), False))

    show_available()
    index = 0
    message = ""
    cursor(0)
    submit = tr("Enter 保存并提问", "Enter to save and ask") if ask_after else tr("Enter 保存", "Enter to save")
    while True:
        height, width = screen.getmaxyx()
        screen.erase()
        draw(screen, 0, 1, tr(f"{title} · 配置 agent", f"{title} · Agent settings"), width - 2, curses.A_BOLD)
        if height >= 6:
            capacity = height - 5
            start = max(0, index - capacity + 1)
            for offset, row in enumerate(rows[start:start + capacity], start):
                agent = row.agent
                checked = "✓" if agent.enabled else " "
                model_name = agent.model_name or localized_label("未命名模型" if agent.model else "默认模型")
                label = f"[{checked}]  {agent.name}    {agent.cli} · {model_name}"
                style = curses.A_REVERSE if offset == index else 0
                draw(screen, offset - start + 2, 1, label, width - 2, style)

        if not rows:
            message = tr(
                "在 PATH 中未找到 Codex、TraeX、Claude Code 或 Doubao",
                "Codex, TraeX, Claude Code, or Doubao not found in PATH",
            )
        draw(screen, height - 3, 1, message, width - 2)
        draw(screen, height - 2, 1, tr(
            "↑↓ 移动  空格勾选  M 改模型  N 改名称  D 复制  X 移除配置",
            "↑↓ Move  Space Toggle  M Model  N Rename  D Duplicate  X Remove",
        ), width - 2, curses.A_DIM)
        draw(screen, height - 1, 1, tr(
            f"{submit}   Ctrl+C 取消",
            f"{submit}   Ctrl+C to cancel",
        ), width - 2, curses.A_DIM)
        screen.refresh()
        key = read_key(screen)
        message = ""

        if key == "\x03":
            raise KeyboardInterrupt
        if key in ENTER:
            if not ask_after or any(row.agent.enabled for row in rows):
                return [row.agent for row in rows if row.configured]
            message = tr("请用空格勾选至少一个 agent", "Press Space to enable at least one agent")
        if not rows:
            continue
        row = rows[index]
        if key in (curses.KEY_UP, curses.KEY_DOWN, "k", "j"):
            step = -1 if key in (curses.KEY_UP, "k") else 1
            index = (index + step) % len(rows)
        elif key == " ":
            row.agent = replace(row.agent, enabled=not row.agent.enabled)
            row.configured = True
        elif key in ("d", "D"):
            name = available_name(row.agent.name, rows)
            index += 1
            rows.insert(index, Row(replace(row.agent, name=name, enabled=True), True))
        elif key in ("x", "X") and row.configured:
            rows.pop(index)
            show_available()
            index = max(0, min(index, len(rows) - 1))
        elif key in ("m", "M"):
            model = asyncio.run(pick_model(screen, row.agent, cwd, catalogs))
            if model is MANUAL:
                value = edit_field(screen, tr("模型 ID", "Model ID"), row.agent.model or "")
                model = (value or None) if value is not None else CANCEL
            if model is not CANCEL:
                catalog = catalogs.get((row.agent.cli, row.agent.executable), {})
                name = catalog.get(model) or (row.agent.model_name if model == row.agent.model else None)
                row.agent = replace(row.agent, model=model, model_name=name)
                row.configured = True
        elif key in ("n", "N"):
            name = edit_field(screen, tr("实例名称", "Agent name"), row.agent.name)
            if name is not None:
                try:
                    if any(other is not row and other.agent.name == name for other in rows):
                        raise ValueError(tr("已存在同名实例", "An agent with that name already exists"))
                    row.agent = replace(row.agent, name=name)
                    row.configured = True
                except ValueError as exc:
                    message = str(exc)


async def pick_model(screen, agent, cwd, catalogs):
    catalog_key = (agent.cli, agent.executable)
    catalog = catalogs.get(catalog_key)
    request = asyncio.create_task(model_choices(agent, cwd)) if catalog is None else None
    selected = agent.model
    status = ""
    screen.timeout(60)
    try:
        while True:
            if request and request.done():
                try:
                    catalog = request.result()
                    catalogs[catalog_key] = catalog
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    status = tr(f"读取模型失败 · {exc}", f"Could not load models · {exc}")
                request = None

            choices = [(None, tr("默认模型（跟随 CLI）", "Default model (from CLI)"))]
            if agent.model and agent.model not in (catalog or {}):
                name = agent.model_name or localized_label("未命名模型")
                choices.append((agent.model, f"{name} · " + tr("当前配置", "Current selection")))
            for model, label in (catalog or {}).items():
                choices.append((model, label or localized_label("未命名模型")))
            choices.append((MANUAL, tr("手动输入模型 ID", "Enter model ID manually")))

            focus = next(index for index, (model, _) in enumerate(choices) if model == selected)
            height, width = screen.getmaxyx()
            capacity = max(1, height - 5)
            start = max(0, focus - capacity + 1)

            screen.erase()
            draw(screen, 0, 1, tr(
                f"选择模型 · {agent.name} ({agent.cli})",
                f"Choose model · {agent.name} ({agent.cli})",
            ), width - 2, curses.A_BOLD)
            for row, (_, label) in enumerate(choices[start:start + capacity], 2):
                style = curses.A_REVERSE if row - 2 + start == focus else 0
                draw(screen, row, 1, f" {label} ", width - 2, style)
            if request:
                progress = tr("正在读取模型列表…", "Loading models…")
            else:
                progress = status or (tr("CLI 没有返回可选模型", "The CLI returned no models") if not catalog else "")
            draw(screen, height - 2, 1, progress, width - 2, curses.A_DIM)
            draw(screen, height - 1, 1, tr(
                "↑↓ 选择   Enter 确认   I 手动输入   Esc 返回",
                "↑↓ Select   Enter Confirm   I Enter manually   Esc Back",
            ), width - 2, curses.A_DIM)
            screen.refresh()

            key = read_key(screen)
            if key in ENTER:
                return selected
            if key == "\x03":
                raise KeyboardInterrupt
            if key == "\x1b":
                return CANCEL
            if key in ("i", "I"):
                return MANUAL
            if key in (curses.KEY_UP, curses.KEY_DOWN, "k", "j"):
                step = -1 if key in (curses.KEY_UP, "k") else 1
                selected = choices[(focus + step) % len(choices)][0]
            if key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                step = -capacity if key == curses.KEY_PPAGE else capacity
                selected = choices[max(0, min(len(choices) - 1, focus + step))][0]
            await asyncio.sleep(0)
    finally:
        screen.timeout(-1)
        if request:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)


def edit_field(screen, label, text):
    index = len(text)
    cursor(1)
    try:
        while True:
            height, width = screen.getmaxyx()
            screen.erase()
            draw(screen, 0, 1, label, width - 2, curses.A_BOLD)
            draw(screen, height - 1, 1, tr(
                "Enter 确认   Esc 返回   Ctrl+U 清空",
                "Enter Confirm   Esc Back   Ctrl+U Clear",
            ), width - 2, curses.A_DIM)
            if height > 3 and width > 6:
                start = index
                columns = 0
                while start > 0 and columns + cells(text[start - 1]) < width - 4:
                    start -= 1
                    columns += cells(text[start])
                draw(screen, 2, 1, text[start:], width - 2)
                screen.move(2, 1 + columns)
            screen.refresh()

            key = read_key(screen)
            if key in ENTER:
                return text.strip()
            if key == "\x03":
                raise KeyboardInterrupt
            if key == "\x1b":
                return None
            if key == "\x15":
                text, index = "", 0
            elif key == curses.KEY_HOME:
                index = 0
            elif key == curses.KEY_END:
                index = len(text)
            elif key == curses.KEY_LEFT:
                index = max(0, index - 1)
            elif key == curses.KEY_RIGHT:
                index = min(len(text), index + 1)
            elif key in (curses.KEY_BACKSPACE, "\b", "\x7f") and index:
                text = text[:index - 1] + text[index:]
                index -= 1
            elif key == curses.KEY_DC:
                text = text[:index] + text[index + 1:]
            elif isinstance(key, str) and key.isprintable():
                text = text[:index] + key + text[index:]
                index += 1
    finally:
        cursor(0)

"""Text composer and independently scrollable response panes."""

import asyncio
import curses
import io
import itertools
import os
import signal
import sys

from .display import cells, clean, wrap
from .history import Exchange
from .localization import label, tr
from .markdown import CODE, render_markdown
from .runners import Activity, Pane


class TerminalHangup(Exception):
    pass


_hung_up = False


def _mark_hangup(_signum, _frame):
    # Let the UI exit at an input boundary so cleanup can finish uninterrupted.
    global _hung_up
    _hung_up = True


def run_with_hangup_handler(callback, *args):
    global _hung_up
    previous = signal.signal(signal.SIGHUP, _mark_hangup)
    try:
        result = callback(*args)
    except (curses.error, OSError):
        if _hung_up:
            raise TerminalHangup from None
        raise
    else:
        if _hung_up:
            raise TerminalHangup
        return result
    finally:
        signal.signal(signal.SIGHUP, previous)
        _hung_up = False


def set_terminal_title(command, prompt):
    first_line = next((line for line in clean(prompt).splitlines() if line.strip()), "")
    summary = " ".join(first_line.split())
    lines = wrap(f"{clean(command)} · {summary}", 64)
    title = lines[0] if len(lines) == 1 else wrap(lines[0], 63)[0] + "…"
    write_control(f"\x1b]2;{title}\x1b\\")


def write_control(sequence):
    # Don't leave a buffered write pending if the terminal disappears.
    stream = sys.stdout
    try:
        fd = stream.fileno()
    except io.UnsupportedOperation:
        stream.write(sequence)
        stream.flush()
    else:
        os.write(fd, sequence.encode(stream.encoding or "utf-8", stream.errors or "strict"))


def draw(screen, row, col, text, width, style=0):
    if width <= 0:
        return
    line = wrap(clean(text), width)[0]
    write_text(screen, row, col, line, style)


def write_text(screen, row, col, text, style):
    try:
        screen.addstr(row, col, text, style)
    except curses.error:
        # Curses reports an error after drawing into the bottom-right cell.
        pass


def inline_code_style():
    try:
        if curses.has_colors() and curses.COLORS >= 256:
            terminal_background = os.environ.get("COLORFGBG", "").rsplit(";", 1)[-1]
            dark = terminal_background in {"0", "1", "2", "3", "4", "5", "6", "8"}
            foreground, background = (252, 236) if dark else (235, 254)
            curses.init_pair(1, foreground, background)
            return curses.color_pair(1)
    except curses.error:
        pass
    return curses.A_UNDERLINE


def cursor(visible):
    try:
        curses.curs_set(visible)
    except curses.error:
        pass


def _check_hangup():
    if _hung_up:
        raise TerminalHangup


def read_key(screen):
    _check_hangup()
    try:
        key = screen.get_wch()
    except curses.error:
        key = None
    _check_hangup()
    return key


def read_paste(screen, timeout):
    screen.keypad(False)
    screen.timeout(60)
    try:
        key = read_key(screen)
        if key != "[":
            return "\n" if key in ("\r", "\n") else None
        if [read_key(screen) for _ in range(4)] != list("200~"):
            return None
        pasted = []
        screen.timeout(1500)
        while True:
            key = read_key(screen)
            if key is None:
                break
            if isinstance(key, str):
                pasted.append(key)
                if pasted[-6:] == list("\x1b[201~"):
                    del pasted[-6:]
                    break
        return "".join(pasted).replace("\r\n", "\n").replace("\r", "\n")
    finally:
        screen.keypad(True)
        screen.timeout(timeout)


async def compose(dashboard, validate_prompt, draft):
    screen = dashboard.screen
    cursor(1)
    prompt, index = draft
    message = ""
    write_control("\x1b[?2004h")
    try:
        while True:
            dashboard.render(draft=prompt, caret=index, message=message)
            key = read_key(screen)
            if key is None:
                await asyncio.sleep(0)
                continue
            message = ""
            if key in ("\r", curses.KEY_ENTER):
                if prompt.strip():
                    try:
                        return validate_prompt(prompt), None
                    except ValueError as exc:
                        message = str(exc)
                continue
            if key == "\x03" or (key == "\x04" and not prompt):
                raise KeyboardInterrupt
            if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                index = min(len(prompt), max(0, index + (-1 if key == curses.KEY_LEFT else 1)))
            elif key == curses.KEY_HOME:
                index = prompt.rfind("\n", 0, index) + 1
            elif key == curses.KEY_END:
                end = prompt.find("\n", index)
                index = len(prompt) if end < 0 else end
            elif key in (curses.KEY_UP, curses.KEY_DOWN):
                start = prompt.rfind("\n", 0, index) + 1
                end = prompt.find("\n", index)
                if key == curses.KEY_UP and start:
                    previous = prompt.rfind("\n", 0, start - 1) + 1
                    index = min(previous + index - start, start - 1)
                elif key == curses.KEY_DOWN and end >= 0:
                    following = prompt.find("\n", end + 1)
                    index = min(end + 1 + index - start, len(prompt) if following < 0 else following)
            elif key in ("\x7f", "\b", curses.KEY_BACKSPACE) and index:
                prompt = prompt[:index - 1] + prompt[index:]
                index -= 1
            elif key == curses.KEY_DC:
                prompt = prompt[:index] + prompt[index + 1:]
            else:
                inserted = ""
                if key == "\x1b":
                    inserted = read_paste(screen, 80)
                    if inserted is None:
                        return None, (prompt, index)
                elif isinstance(key, str) and (key.isprintable() or key in ("\n", "\t")):
                    inserted = key
                prompt = prompt[:index] + inserted + prompt[index:]
                index += len(inserted)
            await asyncio.sleep(0)
    finally:
        try:
            write_control("\x1b[?2004l")
        finally:
            cursor(0)


class Dashboard:
    def __init__(self, screen, title, cwd, panes, exchanges=()):
        self.screen = screen
        self.title = title
        self.cwd = cwd
        self.panes = panes
        self.exchanges = list(exchanges)
        self.focus = 0
        self.scroll = [None] * len(panes)
        self.viewport = [(0, 0, 1)] * len(panes)
        self.archive = {}
        self.signature = None
        self.running = False
        self.notice = ""
        self.code_style = inline_code_style()
        cursor(0)

    def start_reply(self, prompt, images=()):
        self.panes = [Pane(pane.name, pane.model) for pane in self.panes]
        self.exchanges.append(Exchange(prompt, self.panes, images))
        self.scroll = [None] * len(self.panes)
        self.running = True
        self.notice = ""
        self.screen.timeout(0)
        return self.panes

    def history_error(self, message):
        self.notice = message

    async def wait_for_prompt(self, validate_prompt):
        self.running = False
        self.screen.timeout(80)
        editing = True
        draft = ("", 0)
        while True:
            if editing:
                prompt, draft = await compose(self, validate_prompt, draft)
                if prompt is not None:
                    return prompt
                editing = False

            self.render()
            key = read_key(self.screen)
            if key in ("q", "Q"):
                return None
            if key in ("\r", curses.KEY_ENTER):
                editing = True
            else:
                self.navigate(key)
            await asyncio.sleep(0)

    def navigate(self, key):
        if key in ("\t", curses.KEY_BTAB):
            step = 1 if key == "\t" else -1
            self.focus = (self.focus + step) % len(self.panes)
        elif key == curses.KEY_END:
            self.scroll[self.focus] = None
        elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
            start, bottom, capacity = self.viewport[self.focus]
            step = {curses.KEY_UP: -1, curses.KEY_DOWN: 1,
                    curses.KEY_PPAGE: -capacity, curses.KEY_NPAGE: capacity}[key]
            next_start = min(bottom, max(0, start + step))
            self.scroll[self.focus] = None if next_start == bottom else next_start

    def refresh(self):
        key = read_key(self.screen)
        if key == "\x03":
            return False
        self.navigate(key)
        self.render()
        return True

    def render(self, draft=None, caret=0, message=""):
        height, width = self.screen.getmaxyx()
        input_height = min(5, max(1, height // 5))
        input_border = max(1, height - input_height - 2)
        reply_height = input_border - 2
        columns = min(3, len(self.panes), max(1, width // 26))
        rows = 2 if reply_height >= 20 else 1
        page_size = rows * columns
        page, pages = self.focus // page_size, (len(self.panes) + page_size - 1) // page_size
        start = page * page_size
        visible = self.panes[start:start + page_size]
        durations = [pane.duration for pane in visible]
        state = (height, width, self.focus, self.running, len(self.exchanges), tuple(self.scroll), draft, caret,
                 message, self.notice, tuple((pane.status, tuple(pane.entries()), pane.error, pane.model, pane.phase)
                                for pane in visible), tuple(durations))
        if state == self.signature:
            return
        self.signature = state
        self.screen.erase()
        heading = tr(f"{self.title} · 只读", f"{self.title} · Read-only")
        if pages > 1:
            heading += f" · Agent {page + 1}/{pages}"
        draw(self.screen, 0, 1, heading, width - 2, curses.A_BOLD)
        if height > 3:
            draw(self.screen, 1, 1, str(self.cwd), width - 2, curses.A_DIM)
        if reply_height >= 4 and width >= 20:
            actual_rows = (len(visible) + columns - 1) // columns
            panel_height = reply_height // actual_rows
            for offset, (pane, duration) in enumerate(zip(visible, durations)):
                grid_row, grid_column = divmod(offset, columns)
                row = 2 + grid_row * panel_height
                col = grid_column * width // columns
                panel_width = (grid_column + 1) * width // columns - col
                if grid_column:
                    for border_row in range(row, row + panel_height):
                        draw(self.screen, border_row, col, "│", 1, curses.A_DIM)
                self.panel(pane, duration, start + offset, row, col, panel_height, panel_width)

        self.draw_input(draft, caret, message, height, width, input_border)
        self.screen.refresh()

    def draw_input(self, draft, caret, message, height, width, border):
        if height > 3:
            draw(self.screen, border, 1, "─" * max(0, width - 2), width - 2, curses.A_DIM)
        if draft is not None:
            shortcut = tr(
                "Enter 发送   Ctrl+J / Alt+Enter 换行   Esc 浏览",
                "Enter Send   Ctrl+J / Alt+Enter New line   Esc Browse",
            )
        elif self.running:
            shortcut = tr("Ctrl+C 取消   Tab 切换   ↑↓ 滚动", "Ctrl+C Cancel   Tab Switch pane   ↑↓ Scroll")
        else:
            shortcut = tr("Enter 输入   q 退出   Tab 切换   ↑↓ 滚动", "Enter Compose   q Quit   Tab Switch pane   ↑↓ Scroll")
        draw(self.screen, height - 1, 1, message or self.notice or shortcut, width - 2, curses.A_DIM)

        if height < 5 or width < 8:
            return
        row = border + 1
        draw(self.screen, row, 1, "›", 1, curses.A_BOLD)
        draft_width = width - 5
        if draft is None:
            placeholder = tr("等待回复", "Waiting for replies") if self.running else tr("发给所有 agent", "Send to all agents")
            draw(self.screen, row, 3, placeholder, draft_width, curses.A_DIM)
            return

        lines = wrap(draft, draft_width)
        preceding = wrap(draft[:caret], draft_width)
        line = len(preceding) - 1
        col = sum(map(cells, preceding[-1]))
        if col == draft_width:
            line += 1
            col = 0
        capacity = height - row - 1
        start = max(0, line - capacity + 1)
        for line_row, content in enumerate(lines[start:start + capacity], row):
            draw(self.screen, line_row, 3, content, draft_width)
        self.screen.move(row + line - start, 3 + col)

    def exchange_lines(self, exchange, index, width):
        pane = exchange.reply(self.panes[index].name)
        if pane is None:
            return []
        lines = [[(line, curses.A_BOLD)] for line in wrap(clean("› " + exchange.display_prompt), width)]
        for entry in pane.entries():
            if isinstance(entry, Activity):
                text = "↳ " + entry.line
                if entry.state:
                    lines.extend([(line, curses.A_DIM)] for line in wrap(clean(text), width))
                else:
                    lines.extend([(part, style | curses.A_DIM) for part, style in line]
                                 for line in render_markdown(text, width))
            else:
                lines.extend(render_markdown(entry, width))
                lines.append([])
        if pane.status == "运行中" and pane.phase and not any(
            isinstance(entry, Activity) and entry.state == "执行中" for entry in pane.entries()
        ):
            lines.extend([(line, curses.A_DIM)] for line in wrap(label(pane.phase), width))
        if pane.error:
            lines.extend([(line, 0)] for line in wrap(clean(pane.error), width))
        if pane.finished_at is not None or pane.elapsed is not None:
            status = f"{label(pane.status)} · {pane.duration}"
            lines.extend([(line, curses.A_DIM)] for line in wrap(status, width))
        lines.append([])
        return lines

    def transcript(self, index, width):
        if not self.exchanges:
            return [], []
        cached_width, count, older = self.archive.get(index, (None, 0, []))
        if cached_width != width:
            count, older = 0, []
        for exchange in self.exchanges[count:-1]:
            older.extend(self.exchange_lines(exchange, index, width))
        self.archive[index] = (width, len(self.exchanges) - 1, older)
        return older, self.exchange_lines(self.exchanges[-1], index, width)

    def panel(self, pane, duration, index, row, col, height, width):
        focused = index == self.focus
        style = curses.A_BOLD | (curses.A_REVERSE if focused else 0)
        body_width = width - 4
        status = label(pane.status)
        status_width = min(body_width, sum(map(cells, status)))
        draw(self.screen, row, col + 2, pane.name, body_width - status_width - 2, style)
        draw(self.screen, row, col + width - status_width - 2, status, status_width)
        model_width = body_width
        if duration:
            model_width -= len(duration) + 2
            draw(self.screen, row + 1, col + width - len(duration) - 2, duration, len(duration))
        draw(self.screen, row + 1, col + 2, label(pane.model), model_width, curses.A_DIM)

        older, latest = self.transcript(index, body_width)
        capacity = max(1, height - 3)
        bottom = max(0, len(older) + len(latest) - capacity)
        start = bottom if self.scroll[index] is None else min(self.scroll[index], bottom)
        self.viewport[index] = (start, bottom, capacity)
        for row_index, spans in enumerate(
            itertools.islice(itertools.chain(older, latest), start, start + capacity), row + 2
        ):
            col_index = col + 2
            for content, style in spans:
                if style & CODE:
                    style = (style & ~CODE) | self.code_style
                write_text(self.screen, row_index, col_index, content, style)
                col_index += sum(map(cells, content))


class LineOutput:
    def __init__(self, panes):
        self.panes = panes
        self.done = set()

    def history_error(self, message):
        print(message, file=sys.stderr)

    def refresh(self):
        _check_hangup()
        for pane in self.panes:
            if pane.name in self.done or pane.running:
                continue
            self.done.add(pane.name)
            for line in clean(pane.text).splitlines():
                print(f"[{pane.name}] {line}", flush=True)
            print(f"[{pane.name}] {clean(label(pane.model))} · {label(pane.status)} · {pane.duration}",
                  file=sys.stderr, flush=True)
            for error in clean(pane.error).splitlines():
                print(f"[{pane.name}] {error}", file=sys.stderr, flush=True)
        return True

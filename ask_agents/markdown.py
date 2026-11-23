"""Render common Markdown syntax as styled, width-limited terminal lines."""

import curses
import re

from .display import cells, clean

BOLD = curses.A_BOLD
DIM = curses.A_DIM
ITALIC = getattr(curses, "A_ITALIC", curses.A_UNDERLINE)
# The dashboard resolves this flag to a terminal-specific style when drawing.
CODE = 1 << 32
LINK = curses.A_UNDERLINE

INLINE = re.compile(
    r"\\(?P<escape>[\\\x60*_{}\[\]()#+.!|>~\-])"
    r"|(?<!\x60)(?P<tick>\x60+)(?P<code>.+?)(?P=tick)(?!\x60)"
    r"|!?\[(?P<label>[^\]]*)\]\((?P<url><[^>]+>|(?:[^()\s]|\([^()\s]*\))+)"
    r"(?:\s+['\"][^'\"]*['\"])?\)"
    r"|(?P<strong_em>\*\*\*|(?<!\w)___)(?=\S)(?P<both>.*?\S)(?P=strong_em)"
    r"|(?P<strong>\*\*|(?<!\w)__)(?=\S)(?P<bold>.*?\S)(?P=strong)"
    r"|(?<![\w*_])(?P<em>[*_])(?=[^\s*_])(?P<italic>.*?\S)(?P=em)(?!\w)"
    r"|~~(?=\S)(?P<strike>.*?\S)~~"
)
FENCE = re.compile(r"^ {0,3}(\x60{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^ {0,3}#{1,6}(?: +|$)(.*)$")
SETEXT = re.compile(r"^ {0,3}(=+|-+) *$")
RULE = re.compile(r"^ {0,3}(?:(?:- *){3,}|(?:\* *){3,}|(?:_ *){3,})$")
QUOTE = re.compile(r"^ {0,3}> ?(.*)$")
LIST = re.compile(r"^( *)([*+-]|\d{1,9}[.)]) +(.*)$")
TASK = re.compile(r"^\[([ xX])\] +(.*)$")
PIPE = re.compile(r"(?<!\\)\|")
TABLE_SEPARATOR = re.compile(r":?-{3,}:?")


def inline(text, style=0):
    spans = []
    start = 0
    for match in INLINE.finditer(text):
        if match.start() > start:
            spans.append((text[start:match.start()], style))
        if match["escape"] is not None:
            spans.append((match["escape"], style))
        elif match["code"] is not None:
            code = match["code"]
            if code.strip() and code.startswith(" ") and code.endswith(" "):
                code = code[1:-1]
            spans.append((code, style | CODE))
        elif match["url"] is not None:
            url = match["url"].strip("<>")
            label = match["label"] or url
            spans.extend(inline(label, style | LINK))
            if label != url:
                spans.append((f" ({url})", style | DIM))
        elif match["both"] is not None:
            spans.extend(inline(match["both"], style | BOLD | ITALIC))
        elif match["bold"] is not None:
            spans.extend(inline(match["bold"], style | BOLD))
        elif match["italic"] is not None:
            spans.extend(inline(match["italic"], style | ITALIC))
        else:
            spans.extend(inline(match["strike"], style | DIM))
        start = match.end()
    if start < len(text):
        spans.append((text[start:], style))
    return spans


def span_width(spans):
    return sum(cells(char) for text, _ in spans for char in text)


def wrap_spans(spans, width, prefix=(), continuation=None):
    continuation = prefix if continuation is None else continuation
    lines = [list(prefix)]
    used = span_width(prefix)
    indent = span_width(continuation)
    for text, style in spans:
        segment_start = 0
        for index, char in enumerate(text):
            size = cells(char)
            if used + size > width:
                if index > segment_start:
                    lines[-1].append((text[segment_start:index], style))
                lines.append(list(continuation))
                used = indent
                segment_start = index
            used += size
        if segment_start < len(text):
            lines[-1].append((text[segment_start:], style))
    return lines


def table_cells(text):
    parts = PIPE.split(text.strip())
    if len(parts) == 1:
        return []
    if not parts[0]:
        parts = parts[1:]
    if parts and not parts[-1]:
        parts = parts[:-1]
    return [part.strip() for part in parts]


def render_table(headers, values, width):
    count = len(headers)
    header = [inline(cell, BOLD) for cell in headers]
    data = [[inline(cell) for cell in row[:count]] for row in values]
    for row in data:
        row.extend([] for _ in range(count - len(row)))

    space = width - 3 * (count - 1)
    if space < count * 4:
        # A labeled list keeps all cells readable in narrow agent panes.
        lines = []
        for row in data or [[[] for _ in header]]:
            if lines:
                lines.append([])
            for label, cell in zip(header, row):
                lines.extend(wrap_spans(label + ([(" · ", DIM)] + cell if cell else []), width))
        return lines

    rows = [header, *data]
    widths = [max(1, min(space, max(span_width(row[col]) for row in rows))) for col in range(count)]
    excess = sum(widths) - space
    for _ in range(excess):
        widest = max(range(count), key=widths.__getitem__)
        widths[widest] -= 1

    lines = []
    for index, row in enumerate(rows):
        columns = [wrap_spans(cell, size) for cell, size in zip(row, widths)]
        for offset in range(max(map(len, columns))):
            output = []
            for column, size in zip(columns, widths):
                if output:
                    output.append((" │ ", DIM))
                cell = column[offset] if offset < len(column) else []
                output.extend(cell)
                padding = size - span_width(cell)
                if padding:
                    output.append((" " * padding, 0))
            lines.append(output)
        if index == 0:
            lines.append([("─┼─".join("─" * size for size in widths), DIM)])
    return lines


def render_markdown(text, width):
    source = clean(text).split("\n")
    lines = []
    index = 0
    fence = None
    while index < len(source):
        line = source[index]
        index += 1
        if fence:
            marker = line.strip()
            if len(marker) >= len(fence) and set(marker) == {fence[0]}:
                fence = None
            else:
                lines.extend(wrap_spans([(line, 0)], width, [("│ ", DIM)]))
            continue

        match = FENCE.fullmatch(line)
        if match:
            fence, info = match.groups()
            if info.strip():
                lines.extend(wrap_spans([(info.strip(), DIM)], width, [("│ ", DIM)]))
            continue

        if index < len(source):
            headers = [] if line.startswith("    ") else table_cells(line)
            separators = table_cells(source[index]) if headers else []
            if len(headers) == len(separators) and headers and all(TABLE_SEPARATOR.fullmatch(s) for s in separators):
                index += 1
                data = []
                while index < len(source):
                    row = table_cells(source[index])
                    if not row:
                        break
                    data.append(row)
                    index += 1
                lines.extend(render_table(headers, data, width))
                continue

        heading = HEADING.fullmatch(line)
        quote = QUOTE.fullmatch(line)
        bullet = LIST.fullmatch(line)
        if heading:
            title = re.sub(r" +#+ *$", "", heading[1])
            lines.extend(wrap_spans(inline(title, BOLD), width))
        elif RULE.fullmatch(line):
            lines.append([("─" * width, DIM)])
        elif quote:
            lines.extend(wrap_spans(inline(quote[1], ITALIC), width, [("│ ", DIM)]))
        elif bullet:
            indent, marker, body = bullet.groups()
            task = TASK.fullmatch(body)
            if task:
                marker = "☑" if task[1].lower() == "x" else "☐"
                body = task[2]
            elif marker in ("-", "*", "+"):
                marker = "•"
            prefix = (" " * min(len(indent), max(0, width - len(marker) - 4))) + marker + " "
            padding = " " * span_width([(prefix, DIM)])
            lines.extend(wrap_spans(inline(body), width, [(prefix, DIM)], [(padding, 0)]))
        elif line.startswith("    "):
            lines.extend(wrap_spans([(line[4:], 0)], width, [("│ ", DIM)]))
        elif line.strip() and index < len(source) and SETEXT.fullmatch(source[index]):
            lines.extend(wrap_spans(inline(line, BOLD), width))
            index += 1
        else:
            lines.extend(wrap_spans(inline(line), width))
    return lines

"""Shared terminal display helpers."""

import re
import unicodedata

ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\a\x1b]*(?:\a|\x1b\\))")


def clean(text):
    text = ESCAPE.sub("", text).replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return "".join(char for char in text if char == "\n" or not unicodedata.category(char).startswith("C"))


def cells(char):
    if char == "\t":
        return 4
    if unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def wrap(text, width):
    lines = []
    line = []
    used = 0
    for char in text:
        size = cells(char)
        if char == "\n" or used + size > width:
            lines.append("".join(line))
            line = []
            used = 0
        if char != "\n":
            line.append(char)
            used += size
    lines.append("".join(line))
    return lines

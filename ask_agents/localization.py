"""Interface language for CLI invocations and individual desktop requests."""

import os
from contextvars import ContextVar


_language = ContextVar("interface_language", default="zh")

_ENGLISH_LABELS = {
    "默认模型": "Default model",
    "未命名模型": "Unnamed model",
    "就绪": "Ready",
    "等待启动": "Starting",
    "运行中": "Running",
    "完成": "Completed",
    "已取消": "Canceled",
    "已退出": "Left",
    "超时": "Timed out",
    "失败": "Failed",
    "思考中…": "Thinking…",
    "正在回复…": "Responding…",
    "思考摘要": "Reasoning summary",
    "命令": "Command",
    "工具": "Tool",
    "网页搜索": "Web search",
    "查看图片": "View image",
    "文件变更": "File change",
    "执行中": "In progress",
    "已拒绝": "Denied",
    "已完成": "Completed",
    "未报告结果": "No result reported",
    "中断": "Interrupted",
}


def terminal_language():
    override = os.environ.get("ASK_AGENTS_LANGUAGE")
    if override in ("zh", "en"):
        return override
    locale = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG", "")
    return "en" if locale.lower().startswith("en") else "zh"


def set_language(language):
    _language.set("en" if language == "en" else "zh")


def current_language():
    return _language.get()


def tr(chinese, english):
    return english if current_language() == "en" else chinese


def label(value, language=None):
    if (language or current_language()) != "en":
        return value
    if value.startswith("工具 "):
        return "Tool " + value.removeprefix("工具 ")
    return _ENGLISH_LABELS.get(value, value)

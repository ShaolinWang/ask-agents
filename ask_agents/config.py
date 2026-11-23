"""Named agent configurations stored in a user-editable JSON file."""

import fcntl
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .localization import tr

CLIS = ("codex", "traex", "claude", "doubao")


@dataclass(frozen=True)
class Agent:
    name: str
    cli: str
    model: Optional[str] = None
    executable: Optional[str] = None
    enabled: bool = True
    model_name: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,31}", self.name):
            raise ValueError(tr(
                "名称须为 1–32 个字母、数字、点、下划线或连字符，并以字母或数字开头",
                "Names must be 1–32 letters, digits, dots, underscores, or hyphens and start with a letter or digit",
            ))
        if self.cli not in CLIS:
            raise ValueError(tr(
                f"{self.name} 的 CLI 须为 {', '.join(CLIS)} 之一",
                f"The CLI for {self.name} must be one of {', '.join(CLIS)}",
            ))
        if not isinstance(self.enabled, bool):
            raise ValueError(tr(f"{self.name} 的 enabled 须为布尔值", f"The enabled field for {self.name} must be a boolean"))
        for field, value in (("model", self.model), ("executable", self.executable), ("model_name", self.model_name)):
            if value is not None and (
                not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value)
            ):
                raise ValueError(tr(
                    f"{self.name} 的 {field} 须为非空文本",
                    f"The {field} field for {self.name} must be non-empty text",
                ))
        if self.model_name is not None and self.model is None:
            raise ValueError(tr(
                f"{self.name} 的模型名称须随模型 ID 一起保存",
                f"A model ID is required to save a model name for {self.name}",
            ))


def config_path():
    override = os.environ.get("ASK_AGENTS_CONFIG")
    if override:
        return Path(override).expanduser()
    root = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(root).expanduser() / "ask-agents" / "config.json"


def read_config(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (ValueError, OSError) as exc:
        raise ValueError(tr(f"无法读取配置 {path}（{exc}）", f"Could not read config {path} ({exc})")) from exc

    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        raise ValueError(tr(f"{path} 须包含 agents 数组", f"{path} must contain an agents array"))
    try:
        agents = [Agent(**entry) for entry in data["agents"]]
    except (ValueError, TypeError) as exc:
        raise ValueError(tr(f"{path} 的 agent 配置无效（{exc}）", f"Invalid agent configuration in {path} ({exc})")) from exc
    if len({agent.name for agent in agents}) != len(agents):
        raise ValueError(tr(f"{path} 中有重复的 agent 名称", f"Duplicate agent names in {path}"))
    return agents


def update_config(path, edit):
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        agents = edit(read_config(path))
        entries = [
            {key: value for key, value in asdict(agent).items() if value is not None}
            for agent in agents
        ]
        fd, temp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump({"agents": entries}, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)


def edit_config(path, name, replacement=None):
    def edit(current):
        agents = {agent.name: agent for agent in current}
        if replacement:
            agents[name] = replacement
        elif name not in agents:
            raise ValueError(tr(f"找不到配置 {name}", f"Agent {name} was not found"))
        else:
            del agents[name]
        return list(agents.values())

    update_config(path, edit)


def save_config(path, original, updated):
    if original == updated:
        return

    def edit(current):
        if current != original:
            raise ValueError(tr(
                "配置已在另一个终端更新，请重新打开配置界面",
                "Settings changed in another window. Reopen settings to continue.",
            ))
        return updated

    update_config(path, edit)

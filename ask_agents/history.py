"""Local conversation storage shared by the terminal and the macOS app."""

import json
import os
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import uuid4

from .attachments import ImageAttachment
from .config import Agent
from .localization import current_language, tr
from .runners import Activity, AgentConversation, Pane


@dataclass
class Exchange:
    prompt: str
    panes: list[Pane]
    images: tuple[ImageAttachment, ...] = ()

    @property
    def display_prompt(self):
        return self.prompt_with_images(current_language())

    def prompt_with_images(self, language):
        if not self.images:
            return self.prompt
        prefix, separator = ("Images: ", ", ") if language == "en" else ("图片：", "、")
        attachments = prefix + separator.join(image.name for image in self.images)
        return self.prompt + "\n" + attachments if self.prompt.strip() else attachments

    def reply(self, name):
        return next((pane for pane in self.panes if pane.name == name), None)

    def replace_reply(self, pane):
        index = next(index for index, previous in enumerate(self.panes) if previous.name == pane.name)
        self.panes[index] = pane


@dataclass
class Summary:
    id: str
    title: str
    cwd: str
    updated_at: float


@dataclass
class Conversation:
    cwd: Path
    agents: tuple[Agent, ...]
    id: str = field(default_factory=lambda: uuid4().hex)
    exchanges: list[Exchange] = field(default_factory=list)
    saved_turns: int = 0
    exited_agents: set[str] = field(default_factory=set)

    @property
    def active_agents(self):
        return [agent for agent in self.agents if agent.name not in self.exited_agents]

    async def prepare_sessions(self, sessions):
        for agent in self.active_agents:
            previous = sessions.get(agent.name)
            if previous and previous.can_continue:
                continue
            if previous:
                await previous.close()
            sessions[agent.name] = self.session(agent)

    def session(self, agent, before=None):
        context, images = self.context_for(agent.name, before)
        return AgentConversation(agent, self.cwd, context, images)

    def context_for(self, name, before=None):
        def excerpt(text, limit):
            if len(text) <= limit:
                return text
            half = limit // 2
            return text[:half] + "\n…\n" + text[-half:]

        remaining = 48_000
        turns = []
        latest_images = None
        earlier = self.exchanges[:before]
        for offset, exchange in enumerate(reversed(earlier)):
            number = len(earlier) - offset
            pane = exchange.reply(name)
            if pane is None:
                continue
            record = {
                "用户": excerpt(exchange.prompt_with_images("zh"), 8_000),
                "回复": excerpt(pane.text, 12_000),
                "状态": pane.status,
            }
            if exchange.images:
                record["轮次"] = number
            turn = json.dumps(record, ensure_ascii=False)
            if len(turn) > remaining:
                break
            if exchange.images and not latest_images:
                latest_images = (number, exchange.images)
            turns.append(turn)
            remaining -= len(turn) + 1
        return "\n".join(reversed(turns)), latest_images


def encode_pane(pane):
    return {
        "name": pane.name, "model": pane.model, "status": pane.status, "error": pane.error,
        "elapsed": pane.duration,
        "entries": [asdict(entry) if isinstance(entry, Activity) else entry for entry in pane.entries()],
    }


def decode_pane(record):
    entries = [Activity(**entry) if isinstance(entry, dict) else entry for entry in record.pop("entries")]
    return Pane(**record, text="\n\n".join(entry for entry in entries if isinstance(entry, str)),
                timeline=dict(enumerate(entries)))


class History:
    def __init__(self, config):
        config = config.resolve()
        filename = "history.sqlite3" if config.name == "config.json" else config.name + ".history.sqlite3"
        self.path = config.with_name(filename)

    @contextmanager
    def database(self):
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        try:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS conversations (
                        id TEXT PRIMARY KEY, title TEXT NOT NULL, cwd TEXT NOT NULL,
                        agents TEXT NOT NULL, updated_at REAL NOT NULL, turns INTEGER NOT NULL
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS exchanges (
                        conversation_id TEXT NOT NULL, number INTEGER NOT NULL,
                        prompt TEXT NOT NULL, panes TEXT NOT NULL,
                        PRIMARY KEY (conversation_id, number)
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS exited_agents (
                        conversation_id TEXT NOT NULL, name TEXT NOT NULL,
                        PRIMARY KEY (conversation_id, name)
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS exchange_images (
                        conversation_id TEXT NOT NULL, number INTEGER NOT NULL, position INTEGER NOT NULL,
                        name TEXT NOT NULL, media_type TEXT NOT NULL, data BLOB NOT NULL,
                        PRIMARY KEY (conversation_id, number, position)
                    )
                """)
                yield db
        except sqlite3.Error as exc:
            raise ValueError(tr(f"无法访问历史会话（{exc}）", f"Could not access conversation history ({exc})")) from exc

    def list(self):
        if not self.path.exists():
            return []
        with self.database() as db:
            return [Summary(*row) for row in db.execute("""
                SELECT id, title, cwd, updated_at FROM conversations ORDER BY updated_at DESC, id
            """)]

    def open(self, key):
        key = key.lower()
        if not re.fullmatch(r"[a-f0-9]{1,32}", key) or not self.path.exists():
            raise ValueError(tr("找不到该历史会话", "Conversation not found"))
        with self.database() as db:
            db.execute("BEGIN")
            matches = db.execute(
                "SELECT id, cwd, agents FROM conversations WHERE id LIKE ? LIMIT 2", (key + "%",)
            ).fetchall()
            if not matches:
                raise ValueError(tr("找不到该历史会话", "Conversation not found"))
            if len(matches) > 1:
                raise ValueError(tr(
                    "会话 ID 前缀匹配多条记录，请输入更长的 ID",
                    "The prefix matches several conversations. Enter a longer ID.",
                ))
            session_id, cwd, agents = matches[0]
            images = defaultdict(list)
            for number, name, media_type, data in db.execute("""
                SELECT number, name, media_type, data FROM exchange_images
                WHERE conversation_id = ? ORDER BY number, position
            """, (session_id,)):
                images[number].append(ImageAttachment(name, media_type, data))
            exchanges = [
                Exchange(prompt, [decode_pane(pane) for pane in json.loads(panes)], tuple(images[number]))
                for number, prompt, panes in db.execute("""
                    SELECT number, prompt, panes FROM exchanges WHERE conversation_id = ? ORDER BY number
                """, (session_id,))
            ]
            exited = {name for (name,) in db.execute(
                "SELECT name FROM exited_agents WHERE conversation_id = ?", (session_id,)
            )}
        return Conversation(Path(cwd), tuple(Agent(**agent) for agent in json.loads(agents)),
                            session_id, exchanges, len(exchanges), exited)

    def _write_exits(self, db, conversation):
        db.executemany("INSERT OR IGNORE INTO exited_agents VALUES (?, ?)",
                       ((conversation.id, name) for name in conversation.exited_agents))

    def save_exits(self, conversation):
        if conversation.saved_turns:
            with self.database() as db:
                self._write_exits(db, conversation)

    def summary(self, conversation):
        first_line = conversation.exchanges[0].display_prompt.strip().splitlines()[0]
        title = " ".join(first_line.split())
        if len(title) > 80:
            title = title[:79] + "…"
        return Summary(conversation.id, title, str(conversation.cwd), time.time())

    def replace_latest_reply(self, conversation, pane):
        exchange = conversation.exchanges[-1]
        number = len(conversation.exchanges)
        if conversation.saved_turns < number:
            exchange.replace_reply(pane)
            return self._save(conversation)

        previous = exchange.reply(pane.name)
        summary = self.summary(conversation)
        with self.database() as db:
            updated = db.execute("""
                UPDATE conversations SET updated_at = ? WHERE id = ? AND turns = ?
            """, (summary.updated_at, summary.id, number))
            row = db.execute("""
                SELECT panes FROM exchanges WHERE conversation_id = ? AND number = ?
            """, (summary.id, number)).fetchone()
            if not updated.rowcount or row is None:
                raise ValueError(tr(
                    "历史会话已在其他窗口更新，请重新打开后继续",
                    "This conversation changed in another window. Reopen it to continue.",
                ))
            panes = json.loads(row[0])
            index = next((index for index, item in enumerate(panes) if item["name"] == pane.name), None)
            if index is None or previous is None or panes[index] != encode_pane(previous):
                raise ValueError(tr(
                    "该回复已在其他窗口更新，请重新打开后继续",
                    "This reply changed in another window. Reopen the conversation to continue.",
                ))
            panes[index] = encode_pane(pane)
            db.execute("""
                UPDATE exchanges SET panes = ? WHERE conversation_id = ? AND number = ?
            """, (json.dumps(panes), summary.id, number))
        exchange.replace_reply(pane)
        return summary

    def record(self, conversation, exchange):
        conversation.exchanges.append(exchange)
        return self._save(conversation)

    def _save(self, conversation):
        previous = conversation.saved_turns
        total = len(conversation.exchanges)
        summary = self.summary(conversation)
        entries = [
            (conversation.id, number, item.prompt, json.dumps([encode_pane(pane) for pane in item.panes]))
            for number, item in enumerate(conversation.exchanges[previous:], previous + 1)
        ]

        with self.database() as db:
            if previous == 0:
                db.execute("""
                    INSERT INTO conversations (id, title, cwd, agents, updated_at, turns)
                    VALUES (?, ?, ?, ?, ?, 0)
                """, (summary.id, summary.title, summary.cwd,
                      json.dumps([asdict(agent) for agent in conversation.agents]), summary.updated_at))
            updated = db.execute("""
                UPDATE conversations SET updated_at = ?, turns = ? WHERE id = ? AND turns = ?
            """, (summary.updated_at, total, summary.id, previous))
            if not updated.rowcount:
                raise ValueError(tr(
                    "历史会话已在其他窗口更新，请重新打开后继续",
                    "This conversation changed in another window. Reopen it to continue.",
                ))
            db.executemany("INSERT INTO exchanges VALUES (?, ?, ?, ?)", entries)
            db.executemany("INSERT INTO exchange_images VALUES (?, ?, ?, ?, ?, ?)", (
                (conversation.id, number, position, image.name, image.media_type, image.data)
                for number, exchange in enumerate(conversation.exchanges[previous:], previous + 1)
                for position, image in enumerate(exchange.images)
            ))
            self._write_exits(db, conversation)
        conversation.saved_turns = total
        return summary

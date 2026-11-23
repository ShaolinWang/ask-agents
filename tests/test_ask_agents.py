import asyncio
import base64
import curses
import errno
import fcntl
import io
import json
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from contextlib import asynccontextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ask_agents.config import Agent, read_config
from ask_agents.history import Conversation, Exchange, History
from ask_agents.runners import Activity, AgentConversation, Pane, command
from ask_agents.terminal import Dashboard, set_terminal_title


ROOT = Path(__file__).resolve().parents[1]
FAKE_CLI = ROOT / "tests" / "fake_cli.py"
PROMPT = "第一行\n\tsecond line"
FOLLOW_UP = "请结合上次的问题继续"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aYl8AAAAASUVORK5CYII="


class AskAgentsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cwd = Path(temp.name).resolve()
        self.config = self.cwd / "config.json"
        self.command = [str(ROOT / "ask-agents"), "--config", str(self.config), "--language", "zh"]
        self.env = {**os.environ, "ASK_AGENTS_LANGUAGE": "zh", "TERM": "xterm-256color",
                    "EXPECTED_PROMPT": PROMPT, "EXPECTED_FOLLOW_UP": FOLLOW_UP}

    def configure(self, name, cli, model):
        subprocess.run(
            [*self.command, "add", name, cli, "--model", model, "--executable", str(FAKE_CLI)],
            capture_output=True, check=True, timeout=5,
        )

    @asynccontextmanager
    async def desktop(self, **env):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-B", "-m", "ask_agents.desktop", cwd=ROOT,
            env={**self.env, "ASK_AGENTS_CONFIG": str(self.config), **env},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )

        async def send(request_id, action, **fields):
            payload = json.dumps({"id": request_id, "action": action, **fields})
            process.stdin.write(payload.encode() + b"\n")
            await process.stdin.drain()

        async def receive(predicate, *, request_id=None, allow_error=False):
            while line := await asyncio.wait_for(process.stdout.readline(), 5):
                event = json.loads(line)
                if request_id is not None and event["id"] != request_id:
                    continue
                if event["kind"] == "error" and not allow_error:
                    self.fail(event)
                if predicate(event):
                    return event
            self.fail("desktop backend exited before replying")

        try:
            yield send, receive
        finally:
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 5)

    def start_terminal(self, command, env):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
        try:
            child = subprocess.Popen(
                command, cwd=self.cwd, env=env,
                stdin=slave, stdout=slave, stderr=slave,
            )
        finally:
            os.close(slave)
        received = bytearray()

        def read_until(*markers):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if all(marker in received for marker in markers):
                    return True
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break
                    if not chunk:
                        break
                    received.extend(chunk)
            return False

        def stop_child():
            if child.poll() is None:
                os.write(master, b"q\x03")
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()

        self.addCleanup(stop_child)
        return child, master, received, read_until

    def test_asks_directly_with_saved_configuration(self):
        self.configure("first", "codex", "alpha")
        child, master, received, read_until = self.start_terminal(self.command, self.env)
        self.assertTrue(read_until(b"first", b"alpha", b"\x1b[?2004h"), received[-1000:])
        self.assertNotIn("配置 agent".encode(), received)
        received.clear()
        os.write(master, b"\x1b[200~" + PROMPT.encode() + b"\x1b[201~\r")
        title_marker = "\x1b]2;ask-agents · 第一行\x1b\\".encode()
        self.assertTrue(read_until(title_marker, b"\x1b[?2004h"), received[-2000:])
        os.write(master, b"\x1b")
        self.assertTrue(read_until("q 退出".encode()), received[-1000:])
        os.write(master, b"q")
        self.assertTrue(read_until("── first ──".encode(), b"delta-alpha-done\r\n"),
                        received[-2000:])
        self.assertEqual(child.wait(timeout=5), 0)

    def test_terminal_hangup_stops_the_agent_process_group(self):
        self.configure("first", "codex", "alpha")
        lock_path = self.cwd / "agent.lock"
        release = self.cwd / "release"
        env = {**self.env, "AGENT_PROCESS_LOCK": str(lock_path), "RELEASE_FILE": str(release)}
        child, _, received, read_until = self.start_terminal([*self.command, "-p", PROMPT], env)
        self.addCleanup(release.touch)

        self.assertTrue(read_until(b"delta-alpha"), received[-1000:])
        with lock_path.open("r+") as lock:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child.send_signal(signal.SIGHUP)
            self.assertTrue(read_until(b"\x1b[?1049l"), received[-1000:])
            self.assertEqual(child.wait(timeout=5), 128 + signal.SIGHUP)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        listing = subprocess.check_output([*self.command, "history"], text=True, timeout=5)
        session_id = listing.split("\t", 1)[0]
        transcript = subprocess.check_output([*self.command, "history", session_id], text=True, timeout=5)
        self.assertIn("delta-alpha", transcript)
        self.assertIn("已取消", transcript)

    def test_history_reopens_and_continues_across_desktop_and_terminal(self):
        self.configure("first", "codex", "alpha")
        self.configure("second", "traex", "beta")
        subprocess.run([*self.command, "-p", PROMPT], cwd=self.cwd, check=True, timeout=5,
                       capture_output=True, env={**self.env, "EMIT_ACTIVITIES": "1"})
        listing = subprocess.check_output([*self.command, "history"], text=True, timeout=5)
        session_id, title, directory = listing.strip().split("\t")
        self.assertEqual((title, directory), ("第一行", str(self.cwd)))
        transcript = subprocess.check_output([*self.command, "history", session_id[:8]], text=True, timeout=5)
        self.assertIn("已完成 · 命令 · cat activity.txt", transcript)
        self.assertNotIn("PRIVATE_REASONING", transcript)
        self.assertRegex(transcript, r"alpha · 完成 · \d+\.\ds")
        self.config.unlink()
        env = {**self.env, "EXPECTED_RESUME": FOLLOW_UP}

        async def reopen_in_desktop():
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-m", "ask_agents.desktop", cwd=ROOT,
                env={**env, "ASK_AGENTS_CONFIG": str(self.config)},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            )

            async def request(request_id, action, *, kind="result", **fields):
                payload = json.dumps({"id": request_id, "action": action, **fields})
                process.stdin.write(payload.encode() + b"\n")
                await process.stdin.drain()
                while line := await asyncio.wait_for(process.stdout.readline(), 5):
                    event = json.loads(line)
                    self.assertEqual(event["id"], request_id)
                    if event["kind"] != "panes":
                        self.assertEqual(event["kind"], kind, event)
                        return event
                self.fail("desktop backend exited before replying")

            try:
                history = await request(1, "history")
                self.assertEqual([item["id"] for item in history["history"]], [session_id])
                opened = await request(2, "openHistory", conversationID=session_id)
                self.assertEqual(opened["cwd"], str(self.cwd))
                self.assertEqual([agent["model"] for agent in opened["agents"]], ["alpha", "beta"])
                exchange, = opened["exchanges"]
                self.assertEqual(exchange["prompt"], PROMPT)
                self.assertIn({"text": "delta-beta-done"}, exchange["panes"][1]["entries"])
                unavailable = await request(7, "reset", kind="error", agentName="missing")
                self.assertEqual(unavailable["agents"], [])
                followed = await request(3, "ask", agents=opened["agents"], cwd=opened["cwd"],
                                         prompt=FOLLOW_UP, timeout=3)
                self.assertEqual(followed["historyItem"]["id"], session_id)
                new_agents = [{**opened["agents"][0], "model": "gamma", "model_name": "Codex Gamma"}]
                saved = await request(4, "save", original=[], agents=new_agents)
                self.assertEqual(saved["agents"], new_agents)
                single = await request(5, "reset", agentName="first")
                self.assertEqual(single["agents"], new_agents)
                group = await request(6, "reset")
                self.assertEqual(group["agents"], new_agents)
            finally:
                process.stdin.close()
                await asyncio.wait_for(process.wait(), 5)

        asyncio.run(reopen_in_desktop())
        third_question = "继续前两轮"
        resumed = subprocess.run([*self.command, "--resume", session_id, "-p", third_question],
                                 cwd=ROOT, env={**env, "EXPECTED_RESUME": third_question},
                                 text=True, capture_output=True, timeout=5)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertCountEqual(resumed.stdout.splitlines(),
                              ["[first] resumed-alpha-done", "[second] resumed-beta-done"])
        final = subprocess.check_output([*self.command, "history", session_id], text=True, timeout=5)
        self.assertIn(FOLLOW_UP, final)
        self.assertIn(third_question, final)
        self.assertEqual(final.count("resumed-alpha-done"), 2)
        fresh = subprocess.run([*self.command, "-p", PROMPT], cwd=self.cwd, env=self.env,
                               text=True, capture_output=True, timeout=5, check=True)
        self.assertEqual(fresh.stdout.strip(), "[first] delta-gamma-done")

    def test_config_edits_saved_agents_without_asking(self):
        self.configure("first", "codex", "alpha")
        child, master, received, read_until = self.start_terminal([*self.command, "config"], self.env)
        self.assertTrue(read_until("配置 agent".encode()), received[-1000:])
        os.write(master, b" \r")
        self.assertTrue(read_until("配置文件".encode()), received[-1000:])
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertNotIn(b"\x1b[?2004h", received)
        self.assertFalse(json.loads(self.config.read_text())["agents"][0]["enabled"])

    def test_desktop_exits_one_agent_without_interrupting_the_others(self):
        self.configure("first", "codex", "alpha")
        self.configure("second", "traex", "beta")
        self.configure("third", "codex", "gamma")
        agents = json.loads(self.config.read_text())["agents"]
        release = self.cwd / "release"
        archive = History(self.config)

        async def ask_and_exit():
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-m", "ask_agents.desktop", cwd=ROOT,
                env={**self.env, "ASK_AGENTS_CONFIG": str(self.config), "RELEASE_FILE": str(release),
                     "AGENT_PROCESS_LOCK_DIR": str(self.cwd)},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            )

            async def send(request_id, action, **fields):
                payload = json.dumps({"id": request_id, "action": action, **fields})
                process.stdin.write(payload.encode() + b"\n")
                await process.stdin.drain()

            async def receive(predicate):
                while line := await asyncio.wait_for(process.stdout.readline(), 5):
                    event = json.loads(line)
                    if predicate(event):
                        return event
                self.fail("desktop backend exited before replying")

            async def result(request_id):
                event = await receive(lambda event: event["id"] == request_id and event["kind"] != "panes")
                self.assertEqual(event["kind"], "result", event)
                return event

            async def ask(request_id, prompt):
                await send(request_id, "ask", agents=agents, cwd=str(self.cwd), prompt=prompt, timeout=6)

            try:
                await ask(1, PROMPT)
                await receive(lambda event: event["kind"] == "panes" and all(
                    pane["entries"] for pane in event["panes"]))
                await send(2, "exitAgent", agentName="first")
                await result(2)
                with (self.cwd / "alpha").open("r+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with (self.cwd / "beta").open("r+") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                release.touch()
                session_id = (await result(1))["historyItem"]["id"]
                turn = archive.open(session_id).exchanges[0]
                self.assertEqual((turn.reply("first").status, turn.reply("first").text), ("已退出", "delta-alpha"))
                self.assertEqual(turn.reply("second").text, "delta-beta-done")

                await send(3, "exitAgent", agentName="third")
                await result(3)
                self.assertEqual(archive.open(session_id).exited_agents, {"first", "third"})

                await ask(4, FOLLOW_UP)
                self.assertEqual((await result(4))["historyItem"]["id"], session_id)
                follow_up = archive.open(session_id).exchanges[-1]
                self.assertEqual([(pane.name, pane.text) for pane in follow_up.panes],
                                 [("second", "context-beta-done")])

                await send(5, "openHistory", conversationID=session_id)
                self.assertEqual((await result(5))["exitedAgents"], ["first", "third"])
                return session_id
            finally:
                process.stdin.close()
                await asyncio.wait_for(process.wait(), 5)

        session_id = asyncio.run(ask_and_exit())
        self.assertTrue(all(agent.enabled for agent in read_config(self.config)))
        transcript = subprocess.check_output([*self.command, "history", session_id], text=True, timeout=5)
        self.assertIn("── first ──", transcript)
        self.assertIn("── third ──", transcript)
        self.assertIn("已退出", transcript)
        resumed = subprocess.run([*self.command, "--resume", session_id, "-p", FOLLOW_UP], cwd=self.cwd,
                                 env={**self.env, "EXPECTED_RESUME": FOLLOW_UP}, text=True,
                                 capture_output=True, check=True, timeout=5)
        self.assertEqual(resumed.stdout.strip(), "[second] resumed-beta-done")

    def test_desktop_retry_replaces_one_reply_and_preserves_the_round(self):
        self.configure("first", "codex", "alpha")
        self.configure("second", "traex", "beta")
        agents = json.loads(self.config.read_text())["agents"]
        follow_up_release = self.cwd / "allow-beta"
        retry_release = self.cwd / "allow-retry"
        history = History(self.config)

        async def exercise():
            async with self.desktop(RETRY_MODEL="alpha", RETRY_RELEASE_FILE=str(retry_release),
                                    BLOCK_FOLLOW_UP_MODEL="beta",
                                    FOLLOW_UP_RELEASE_FILE=str(follow_up_release)) as (send, receive):
                async def ask(request_id, prompt):
                    await send(request_id, "ask", agents=agents, cwd=str(self.cwd), prompt=prompt, timeout=8)

                async def result(request_id):
                    return await receive(lambda event: event["kind"] == "result", request_id=request_id)

                await ask(1, PROMPT)
                session_id = (await result(1))["historyItem"]["id"]
                await ask(2, FOLLOW_UP)
                await receive(lambda event: event["kind"] == "panes" and
                              [pane["status"] for pane in event["panes"]] == ["完成", "运行中"],
                              request_id=2)
                await send(3, "retryAgent", agentName="first", timeout=8)
                await receive(lambda event: event["kind"] == "panes" and
                              event["panes"][0]["entries"] == [{"text": "retry-alpha"}], request_id=3)

                follow_up_release.touch()
                self.assertEqual((await result(2))["historyItem"]["id"], session_id)
                in_progress = history.open(session_id)
                self.assertEqual(len(in_progress.exchanges), 2)
                self.assertEqual(in_progress.exchanges[-1].reply("second").text, "context-beta-done")
                self.assertEqual(in_progress.exchanges[-1].reply("first").text, "context-alpha-done")

                retry_release.touch()
                self.assertEqual((await result(3))["historyItem"]["id"], session_id)
                await send(4, "openHistory", conversationID=session_id)
                restored = await result(4)
                self.assertEqual(len(restored["exchanges"]), 2)
                self.assertEqual([pane["entries"] for pane in restored["exchanges"][-1]["panes"]],
                                 [[{"text": "retry-alpha-done"}], [{"text": "context-beta-done"}]])
                await send(5, "retryAgent", agentName="first", timeout=8)
                self.assertEqual((await result(5))["historyItem"]["id"], session_id)

        asyncio.run(exercise())
        self.assertEqual(len(history.list()), 1)

    def test_desktop_keeps_images_for_history_and_retry(self):
        for name, model in [("codex", "alpha"), ("traex", "beta"), ("claude", "gamma"), ("doubao", "delta")]:
            self.configure(name, name, model)
        agents = json.loads(self.config.read_text())["agents"]
        images = [{"name": "屏幕截图.png", "data": PNG}]

        async def exercise():
            async with self.desktop(EXPECTED_PROMPT="", EXPECTED_IMAGES=json.dumps([PNG])) as (send, receive):
                async def result(request_id):
                    return await receive(lambda event: event["kind"] == "result", request_id=request_id)

                await send(1, "ask", agents=agents, cwd=str(self.cwd), prompt="", images=images, timeout=5)
                item = (await result(1))["historyItem"]
                self.assertEqual(item["title"], "图片：屏幕截图.png")
                await send(2, "openHistory", conversationID=item["id"])
                exchange, = (await result(2))["exchanges"]
                self.assertEqual(exchange["images"], images)
                panes = {pane["name"]: pane for pane in exchange["panes"]}
                for name in ("codex", "traex", "claude"):
                    self.assertEqual(panes[name]["status"], "完成", panes[name])
                self.assertEqual(panes["doubao"]["status"], "失败")
                self.assertIn("仅支持文本", panes["doubao"]["error"])

                await send(3, "retryAgent", agentName="codex", timeout=5)
                self.assertEqual((await result(3))["historyItem"]["id"], item["id"])
                loaded = History(self.config).open(item["id"]).exchanges[0]
                self.assertEqual(loaded.reply("codex").status, "完成")
                self.assertEqual(loaded.images[0].data, base64.b64decode(PNG))

        asyncio.run(exercise())

    def test_desktop_can_follow_up_after_canceling_a_reply(self):
        self.configure("first", "codex", "alpha")
        agents = json.loads(self.config.read_text())["agents"]
        question = "取消后继续"

        async def exercise():
            async with self.desktop(EXPECTED_RECOVERY=question, BLOCK_FOLLOW_UP_MODEL="alpha",
                                    FOLLOW_UP_RELEASE_FILE=str(self.cwd / "never-release")) as (send, receive):
                async def ask(request_id, prompt):
                    await send(request_id, "ask", agents=agents, cwd=str(self.cwd), prompt=prompt, timeout=8)

                await ask(1, PROMPT)
                first = await receive(lambda event: event["kind"] == "result", request_id=1)
                await ask(2, FOLLOW_UP)
                await receive(lambda event: event["kind"] == "panes" and
                              {"text": "context-alpha"} in event["panes"][0]["entries"], request_id=2)
                await send(3, "cancel")
                await receive(lambda event: event["kind"] == "cancelled", request_id=2)
                await ask(4, question)
                third = await receive(lambda event: event["kind"] == "result", request_id=4)
                session_id = first["historyItem"]["id"]
                self.assertEqual(third["historyItem"]["id"], session_id)
                conversation = History(self.config).open(session_id)
                self.assertEqual(conversation.exchanges[-1].reply("first").text, "resumed-alpha-done")

        asyncio.run(exercise())

    def test_terminal_cancel_returns_to_composer_and_browsing_preserves_draft(self):
        self.configure("first", "codex", "alpha")
        question = "continue after cancellation"
        env = {**self.env, "EXPECTED_RECOVERY": question, "BLOCK_FOLLOW_UP_MODEL": "alpha",
               "FOLLOW_UP_RELEASE_FILE": str(self.cwd / "never-release")}
        child, master, received, read_until = self.start_terminal([*self.command, "-p", PROMPT], env)
        self.assertTrue(read_until(b"delta-alpha-done", b"\x1b[?2004h"), received[-1000:])
        received.clear()
        os.write(master, FOLLOW_UP.encode() + b"\r")
        self.assertTrue(read_until(b"context-alpha"), received[-1000:])
        received.clear()
        os.write(master, b"\x03")
        self.assertTrue(read_until(b"\x1b[?2004h"), received[-1000:])
        received.clear()
        os.write(master, question.encode() + b"\x1b")
        self.assertTrue(read_until("q 退出".encode()), received[-1000:])
        received.clear()
        os.write(master, b"\r")
        self.assertTrue(read_until(b"\x1b[?2004h", question.encode()), received[-1000:])
        received.clear()
        os.write(master, b"\r")
        self.assertTrue(read_until(b"resumed-alpha-done", b"\x1b[?2004h"), received[-1000:])
        received.clear()
        os.write(master, b"\x1b")
        self.assertTrue(read_until("q 退出".encode()), received[-1000:])
        received.clear()
        os.write(master, b"q")
        self.assertTrue(read_until("── first ──".encode(), b"resumed-alpha-done\r\n"), received[-1000:])
        self.assertEqual(child.wait(timeout=5), 1)

    def test_saved_reply_does_not_change_when_retry_cannot_write_history(self):
        history = History(self.config)
        conversation = Conversation(self.cwd, (Agent("first", "codex"),))
        original = Pane("first", "default", status="完成", text="original")
        history.record(conversation, Exchange(PROMPT, [original]))
        replacement = Pane("first", "default", status="完成", text="replacement")
        with patch.object(history, "database", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(OSError, "write failed"):
                history.replace_latest_reply(conversation, replacement)
        self.assertEqual(conversation.exchanges[-1].reply("first").text, "original")
        history.replace_latest_reply(conversation, replacement)
        self.assertEqual(history.open(conversation.id).exchanges[-1].reply("first").text, "replacement")

    def test_terminal_attaches_file_to_text_and_displays_it_in_history(self):
        self.configure("first", "codex", "alpha")
        image = self.cwd / "screen shot.png"
        image.write_bytes(base64.b64decode(PNG))
        result = subprocess.run(
            [*self.command, "--plain", "--image", str(image), "-p", PROMPT],
            cwd=self.cwd, env={**self.env, "EXPECTED_IMAGES": json.dumps([PNG])},
            text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        history = History(self.config)
        session_id = history.list()[0].id
        image.unlink()
        transcript = subprocess.check_output([*self.command, "history", session_id], text=True, timeout=5)
        self.assertIn("图片：screen shot.png", transcript)
        resumed = subprocess.run(
            [*self.command, "--plain", "--resume", session_id, "-p", FOLLOW_UP],
            cwd=self.cwd, env={**self.env, "EXPECTED_IMAGES": json.dumps([PNG]), "EXPECTED_RESUME": FOLLOW_UP,
                               "EXPECTED_HISTORY_IMAGE_NAME": image.name},
            text=True, capture_output=True, timeout=5,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("[first] resumed-alpha-done", resumed.stdout)

    def test_repeated_cli_with_separate_models(self):
        self.configure("first", "codex", "alpha")
        self.configure("second", "codex", "beta")
        self.configure("first", "codex", "alpha-new")
        records = json.loads(self.config.read_text())["agents"]
        self.assertEqual([(entry["name"], entry["model"]) for entry in records],
                         [("first", "alpha-new"), ("second", "beta")])
        result = subprocess.run(self.command, cwd=self.cwd, input=PROMPT, text=True,
                                env=self.env, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertCountEqual(result.stdout.splitlines(),
                              ["[first] delta-alpha-new-done", "[second] delta-beta-done"])
        self.assertRegex(result.stderr, r"\[first\] alpha-new · 完成 · \d+\.\ds")
        self.assertRegex(result.stderr, r"\[second\] beta · 完成 · \d+\.\ds")

    def test_unsafe_sandbox_does_not_start_turn(self):
        self.configure("rejected", "codex", "unsafe")
        self.configure("working", "traex", "beta")
        result = subprocess.run([*self.command, "--prompt", PROMPT], cwd=self.cwd, env=self.env,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("[rejected] CLI 未确认只读模式", result.stderr)
        self.assertIn("[working] delta-beta-done", result.stdout)
        self.assertNotIn("delta-unsafe", result.stdout)

    def test_terminal_title_uses_a_clean_and_short_first_line(self):
        output = io.StringIO()
        with redirect_stdout(output):
            set_terminal_title("asa", "\n  第一行 \x1b]2;ignored\a  " + "长" * 40 + "\nsecond line")

        self.assertEqual(output.getvalue(), "\x1b]2;asa · 第一行 " + "长" * 25 + "…\x1b\\")

    def test_asa_help_uses_installed_name(self):
        alias = self.cwd / "asa"
        alias.symlink_to(ROOT / "ask-agents")
        result = subprocess.run([str(alias), "--help"], capture_output=True, text=True, check=True, timeout=5)
        self.assertTrue(result.stdout.startswith("usage: asa "), result.stdout)

    def test_terminal_language_switch_preserves_saved_conversation(self):
        self.configure("first", "codex", "alpha")
        command = [str(ROOT / "ask-agents"), "--config", str(self.config)]
        env = {**self.env, "ASK_AGENTS_LANGUAGE": "en", "EMIT_ACTIVITIES": "1"}
        help_text = subprocess.check_output([*command, "--help"], env=env, text=True, timeout=5)
        self.assertIn("Ask several agents in parallel", help_text)
        chinese_help = subprocess.check_output([*command, "--language", "zh", "--help"], env=env, text=True, timeout=5)
        self.assertIn("并行向多个 agent 提问", chinese_help)

        reply = subprocess.run([*command, "--plain", "-p", PROMPT], cwd=self.cwd, env=env,
                               text=True, capture_output=True, check=True, timeout=5)
        self.assertIn("alpha · Completed", reply.stderr)
        item, = History(self.config).list()
        for language, activity in [("zh", "已完成 · 命令"), ("en", "Completed · Command")]:
            transcript = subprocess.check_output([*command, "--language", language, "history", item.id],
                                                 env=env, text=True, timeout=5)
            self.assertIn(activity + " · cat activity.txt", transcript)
            self.assertIn(PROMPT.splitlines()[0], transcript)
            self.assertIn("delta-alpha-done", transcript)

    def test_desktop_language_is_applied_per_request(self):
        self.configure("first", "codex", "alpha")
        agents = json.loads(self.config.read_text())["agents"]

        async def exercise():
            async with self.desktop(EMIT_ACTIVITIES="1") as (send, receive):
                for request_id, language, message in [
                    (1, "en", "Enter a question or attach an image"),
                    (2, "zh", "请输入问题或添加图片"),
                ]:
                    await send(request_id, "ask", language=language, agents=agents, cwd=str(self.cwd), prompt="", timeout=5)
                    failure = await receive(lambda event: event["kind"] == "error", request_id=request_id,
                                            allow_error=True)
                    self.assertEqual(failure["message"], message)
                await send(3, "ask", language="en", agents=agents, cwd=str(self.cwd), prompt=PROMPT, timeout=5)
                result = await receive(lambda event: event["kind"] == "result", request_id=3)
                await send(4, "openHistory", language="zh", conversationID=result["historyItem"]["id"])
                reopened = await receive(lambda event: event["kind"] == "result", request_id=4)
                pane, = reopened["exchanges"][0]["panes"]
                self.assertEqual((pane["status"], pane["statusEnglish"]), ("完成", "Completed"))
                self.assertIn({"activity": "已完成 · 命令 · cat activity.txt",
                               "activityEnglish": "Completed · Command · cat activity.txt"}, pane["entries"])
                self.assertIn({"text": "delta-alpha-done"}, pane["entries"])

        asyncio.run(exercise())

    def test_dashboard_renders_chat_history_in_three_columns(self):
        class Screen:
            def __init__(self):
                self.text = {}
                self.styles = {}

            def getmaxyx(self):
                return 30, 120

            def timeout(self, value):
                pass

            def erase(self):
                self.text.clear()
                self.styles.clear()

            def addstr(self, row, col, text, style):
                self.text[(row, col)] = text
                self.styles[(row, col)] = style

            def refresh(self):
                pass

            def move(self, row, col):
                pass

            def get_wch(self):
                raise curses.error

        screen = Screen()
        names = ("first", "second", "third")
        dashboard = Dashboard(screen, "asa", self.cwd, [Pane(name, "model") for name in names])
        panes = dashboard.start_reply(PROMPT)
        for pane in panes:
            pane.text = f"**{pane.name} initial reply**"
        panes[0].started_at = 10.0
        panes[1].started_at, panes[1].finished_at = 10.0, 11.0

        with patch("ask_agents.runners.time.monotonic", return_value=12.5):
            dashboard.refresh()
        self.assertEqual([screen.text[(6, col)] for col in (2, 42, 82)],
                         [f"{name} initial reply" for name in names])
        self.assertEqual([screen.styles[(6, col)] for col in (2, 42, 82)], [curses.A_BOLD] * 3)
        self.assertEqual([screen.text[(3, col)] for col in (34, 74)], ["2.5s", "1.0s"])

        with patch("ask_agents.runners.time.monotonic", return_value=13.5):
            dashboard.refresh()
        self.assertEqual([screen.text[(3, col)] for col in (34, 74)], ["3.5s", "1.0s"])

        panes[0].finished_at = 13.5
        follow_ups = dashboard.start_reply(FOLLOW_UP)
        for pane in follow_ups:
            pane.text = f"{pane.name} second reply"

        dashboard.render(draft="shared draft", caret=12)
        for name, col in zip(names, (2, 42, 82)):
            transcript = [text for (row, position), text in sorted(screen.text.items()) if position == col]
            self.assertLess(transcript.index(f"{name} initial reply"), transcript.index("› " + FOLLOW_UP))
            self.assertLess(transcript.index("› " + FOLLOW_UP), transcript.index(f"{name} second reply"))
            self.assertIn("› 第一行", transcript)
        self.assertIn("shared draft", screen.text.values())

    def test_print_clis_keep_conversation_context(self):
        def doubao_fixture(agent, cwd, resume=None):
            return [str(FAKE_CLI), "--fake-doubao", *(["--resume", resume] if resume else [])]

        async def check_agent(cli):
            session = AgentConversation(Agent(cli, cli, executable=str(FAKE_CLI)), self.cwd)
            try:
                first = Pane(cli, "default")
                await session.run(first, PROMPT, 3)
                self.assertEqual((first.status, first.text), ("完成", f"{cli}-first"), first.error)
                state = "已完成" if cli == "claude" else "失败"
                self.assertEqual(list(first.entries()), [Activity("工具 Read", "activity.txt", state), f"{cli}-first"])
                follow_up = Pane(cli, "default")
                await session.run(follow_up, FOLLOW_UP, 3)
                self.assertEqual(
                    (follow_up.status, follow_up.text), ("完成", f"{cli}-with-context"), follow_up.error
                )
            finally:
                await session.close()

        with patch.dict(os.environ, self.env):
            asyncio.run(check_agent("claude"))
            with patch("ask_agents.runners.command", side_effect=doubao_fixture):
                asyncio.run(check_agent("doubao"))

    @unittest.skipUnless(Path("/usr/bin/sandbox-exec").is_file(), "requires macOS sandbox-exec")
    def test_doubao_root_keeps_temp_writable_and_other_paths_read_only(self):
        temp = self.cwd / "allowed-temp"
        temp.mkdir()
        agent = Agent("doubao", "doubao", executable="/usr/bin/touch")
        cases = [
            (Path("/"), temp / "root-temp", True),
            (Path("/"), self.cwd / "root-outside-temp", False),
            (temp, temp / "project-temp", False),
        ]
        with patch("ask_agents.runners.tempfile.gettempdir", return_value=str(temp)):
            for cwd, target, writable in cases:
                with self.subTest(cwd=cwd, target=target.name):
                    argv = command(agent, cwd)
                    probe = [*argv[:argv.index(agent.executable) + 1], str(target)]
                    result = subprocess.run(probe, cwd=cwd, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode == 0, writable, result.stderr)
                    self.assertEqual(target.exists(), writable)

    @unittest.skipUnless(Path("/usr/bin/sandbox-exec").is_file(), "requires macOS sandbox-exec")
    def test_doubao_can_bind_shared_service_and_create_chat_directory(self):
        agent = Agent("doubao", "doubao", executable=sys.executable)
        operations = {
            "bind": "import socket, sys; listener = socket.socket(socket.AF_UNIX); listener.bind(sys.argv[1])",
            "mkdir": "from pathlib import Path; import sys; Path(sys.argv[1]).mkdir(parents=True)",
            "write": "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('test')",
        }
        with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
            outside = Path(directory)
            control = outside / "control"
            control.mkdir()
            chat = outside / "Doubao" / "chats" / "new-chat"
            with patch("ask_agents.runners.DOUBAO_CONTROL_DIR", control), \
                    patch("ask_agents.runners.Path.home", return_value=outside), \
                    patch("ask_agents.runners.tempfile.gettempdir", return_value=str(self.cwd)):
                argv = command(agent, Path("/"))
            probe = [*argv[:argv.index(agent.executable) + 1], "-B", "-c"]
            cases = [
                ("bind", control / "service.sock", True),
                ("bind", outside / "service.sock", False),
                ("mkdir", chat, True),
                ("write", chat / "file.txt", False),
                ("mkdir", outside / "Doubao" / "elsewhere", False),
            ]
            for operation, path, writable in cases:
                with self.subTest(operation=operation, path=path.name):
                    result = subprocess.run([*probe, operations[operation], str(path)], capture_output=True, timeout=5)
                    self.assertEqual(result.returncode == 0, writable, result.stderr)

    def test_first_run_and_follow_up_reuse_each_selected_agent(self):
        binaries = self.cwd / "bin"
        binaries.mkdir()
        for name in ("codex", "traex"):
            (binaries / name).symlink_to(FAKE_CLI)
        alias = binaries / "asa"
        alias.symlink_to(ROOT / "ask-agents")
        release = self.cwd / "release"
        env = {
            **self.env, "PATH": f"{binaries}{os.pathsep}{self.env['PATH']}",
            "RELEASE_FILE": str(release), "EMIT_ACTIVITIES": "1",
        }
        child, master, received, read_until = self.start_terminal(["asa", "--config", str(self.config)], env)
        self.addCleanup(release.touch)

        self.assertTrue(read_until("asa · 配置 agent".encode()), received[-1000:])
        os.write(master, b" m")
        self.assertTrue(read_until(b"Codex Alpha", b"Codex Gamma"), received[-1000:])
        os.write(master, b"j\rdmj\rj m")
        self.assertTrue(read_until(b"Trae Beta"), received[-1000:])
        os.write(master, b"j\r\r")
        self.assertTrue(read_until(b"\x1b[?2004h"), received[-1000:])
        saved = json.loads(self.config.read_text())["agents"]
        self.assertEqual(
            [(agent["name"], agent["model"]) for agent in saved],
            [("codex", "alpha"), ("codex-2", "gamma"), ("traex", "beta")],
        )
        self.assertEqual([agent["model_name"] for agent in saved], ["Codex Alpha", "Codex Gamma", "Trae Beta"])
        os.write(master, b"\x1b[200~" + PROMPT.encode() + b"\x1b[201~\r")
        first_title = "\x1b]2;asa · 第一行\x1b\\".encode()
        self.assertTrue(read_until(first_title, "思考摘要".encode(), "执行中".encode(),
                                   b"cat activity.txt", b"delta-alpha", b"delta-beta", b"delta-gamma"),
                        received[-3000:])
        self.assertIsNone(child.poll())
        self.assertNotIn(b"\x1b[40m", received)

        received.clear()
        release.touch()
        self.assertTrue(read_until(b"\x1b[?2004h"), received[-2000:])
        received.clear()
        os.write(master, FOLLOW_UP.encode() + b"\r")
        follow_up_title = f"\x1b]2;asa · {FOLLOW_UP}\x1b\\".encode()
        self.assertTrue(read_until(follow_up_title, b"context-alpha", b"context-beta", b"context-gamma",
                                   b"\x1b[?2004h"), received[-3000:])
        self.assertNotIn(b"late-previous-turn", received)

        received.clear()
        os.write(master, b"\x1b")
        self.assertTrue(read_until("q 退出".encode()), received[-2000:])
        os.write(master, b"q")
        self.assertTrue(read_until("── traex ──".encode(), b"context-beta-done\r\n"), received[-2000:])
        self.assertEqual(child.wait(timeout=5), 0)
        transcript = received.decode("utf-8", errors="replace")
        start = transcript.index("── codex ──")
        end = transcript.index("── codex-2 ──")
        codex = transcript[start:end]
        self.assertNotIn("PRIVATE_REASONING", transcript)
        self.assertLess(codex.index("思考摘要 · Inspecting activity.txt"), codex.index("已完成 · 命令 · cat activity.txt"))
        self.assertLess(codex.index("已完成 · 命令 · cat activity.txt"), codex.index("delta-alpha-done"))
        self.assertLess(codex.index("delta-alpha-done"), codex.index(FOLLOW_UP))
        self.assertLess(codex.index(FOLLOW_UP), codex.index("context-alpha-done"))

"""herd server - owns the agents' terminals so they keep running after the window is closed.

Started automatically by `herd app`; stopped with `herd stop` (or prefix then Q in the app).
"""

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from multiprocessing.connection import Listener
from pathlib import Path

import pyte

from agent_manager.ipc import HERD_HOME, address, authkey, try_connect

if os.name == "nt":
    from winpty import PtyProcess
else:  # lets herd run on Linux/macOS too, handy for development
    from ptyprocess import PtyProcessUnicode as PtyProcess

HERD_DIR = HERD_HOME / "sessions"
SCROLLBACK = 10000
BRACKETED_PASTE = 2004 << 5  # how pyte stores private mode ?2004


def suggested_space_name(cwd):
    """Use the branch for feature work, the repo name for its default branch."""
    def git_output(*args):
        try:
            result = subprocess.run(
                ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    root = git_output("rev-parse", "--show-toplevel")
    if root:
        branch = git_output("symbolic-ref", "--quiet", "--short", "HEAD")
        if branch and branch.casefold() not in {"main", "master"}:
            return branch
        return Path(root).name or "workspace"
    return Path(cwd).name or "workspace"


class TermScreen(pyte.HistoryScreen):
    """pyte screen that answers terminal queries (cursor position etc.) back to the agent."""

    def __init__(self, columns, lines, reply):
        super().__init__(columns, lines, history=SCROLLBACK, ratio=0.5)
        self._reply = reply

    def write_process_input(self, data):
        self._reply(data)


class Agent:
    def __init__(self, space, name, cwd, command):
        self.id = uuid.uuid4().hex[:12]
        self.space = space  # the space dict this agent's tab lives in
        self.name = name
        self.cwd = cwd
        self.command = command
        self.state = "idle" if not command.strip() else "starting"
        self.since = time.time()
        self.alive = False
        self.version = 0  # bumped whenever the screen changes
        self.lock = threading.Lock()
        self.screen = TermScreen(80, 24, self.write)
        self.stream = pyte.Stream(self.screen)
        self.proc = None

    def argv(self):
        if not self.command.strip():
            if os.name == "nt":
                if shutil.which("pwsh"):
                    return "pwsh"
                if shutil.which("powershell"):
                    return "powershell"
                return os.environ.get("COMSPEC", "cmd.exe")
            return [os.environ.get("SHELL") or "/bin/sh"]
        if os.name == "nt":
            resolved = shutil.which(self.command.split()[0])
            # npm installs claude as a .cmd shim, which has to go through cmd.exe
            if resolved and resolved.lower().endswith((".cmd", ".bat")):
                return f"cmd.exe /c {self.command}"
            return self.command
        return ["/bin/sh", "-c", self.command]

    def start(self, rows, cols):
        with self.lock:
            self.screen.resize(rows, cols)
        env = dict(os.environ, HERD_ID=self.id, HERD_NAME=f"{self.space['name']}/{self.name}",
                   TERM="xterm-256color", COLORTERM="truecolor")
        self.proc = PtyProcess.spawn(self.argv(), cwd=self.cwd, env=env, dimensions=(rows, cols))
        self.alive = True
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        while True:
            try:
                data = self.proc.read(65536)
            except (EOFError, OSError):
                break
            if data:
                with self.lock:
                    self.stream.feed(data)
                    self.version += 1
        self.alive = False
        self.set_state("exited")
        self.version += 1

    def write(self, data):
        if self.alive:
            try:
                self.proc.write(data)
            except (EOFError, OSError):
                pass

    def paste(self, text):
        if BRACKETED_PASTE in self.screen.mode:
            text = f"\x1b[200~{text}\x1b[201~"
        self.write(text)

    def resize(self, rows, cols):
        with self.lock:
            if (rows, cols) == (self.screen.lines, self.screen.columns):
                return
            self.screen.resize(rows, cols)
            self.version += 1
        if self.alive:
            try:
                self.proc.setwinsize(rows, cols)
            except Exception:
                pass

    def set_state(self, state):
        if state != self.state:
            self.since = time.time()
        self.state = state

    def kill(self):
        if self.alive:
            try:
                self.proc.terminate(force=True)
            except Exception:
                pass

    def meta(self):
        return {"id": self.id, "space": self.space["id"], "name": self.name, "cwd": self.cwd,
                "state": self.state, "since": self.since}

    def snapshot(self, offset):
        """The visible screen (or a scrolled-back view) as rows of (text, style) runs."""
        with self.lock:
            screen = self.screen
            history = screen.history.top
            offset = min(offset, len(history))
            show_cursor = offset == 0 and not screen.cursor.hidden
            rows = []
            for y in range(screen.lines):
                index = len(history) - offset + y
                if index < len(history):
                    row, cursor_x = history[index], -1
                else:
                    row = screen.buffer[index - len(history)]
                    cursor_x = screen.cursor.x if show_cursor and screen.cursor.y == y else -1
                runs, text, key = [], [], None
                for x in range(screen.columns):
                    char = row[x]
                    if char.data == "":  # right half of a wide character
                        continue
                    k = (char.fg, char.bg, char.bold, char.italics, char.underscore,
                         char.strikethrough, char.reverse ^ (x == cursor_x))
                    if k != key and text:
                        runs.append(("".join(text), key))
                        text = []
                    key = k
                    text.append(char.data)
                if text:
                    runs.append(("".join(text), key))
                rows.append(runs)
            return {"id": self.id, "rows": rows, "offset": offset, "history": len(history)}


class ClientConn:
    def __init__(self, conn):
        self.conn = conn
        self.lock = threading.Lock()
        self.view = None
        self.offset = 0
        self.sent_screen = None
        self.sent_meta = None

    def send(self, message):
        with self.lock:
            try:
                self.conn.send(message)
            except Exception:
                pass


class Server:
    def __init__(self):
        self.spaces = []  # [{"id", "name", "cwd"}], in sidebar order
        self.agents = {}  # id -> Agent, in tab order
        self.clients = []
        self.size = (24, 80)
        self.lock = threading.Lock()

    def serve(self):
        if os.name != "nt" and os.path.exists(address()):
            os.unlink(address())  # stale socket from a server that died
        listener = Listener(address(), authkey=authkey())
        print(f"herd server listening on {address()}", flush=True)
        threading.Thread(target=self._tick_loop, daemon=True).start()
        while True:
            try:
                conn = listener.accept()
            except Exception:  # e.g. a client with the wrong key
                continue
            client = ClientConn(conn)
            with self.lock:
                self.clients.append(client)
            threading.Thread(target=self._client_loop, args=(client,), daemon=True).start()

    def _client_loop(self, client):
        while True:
            try:
                message = client.conn.recv()
            except Exception:
                break
            try:
                self.handle(client, message)
            except Exception as e:
                client.send(("error", str(e)))
        with self.lock:
            self.clients.remove(client)

    def handle(self, client, message):
        kind, *args = message
        if kind == "input":
            agent = self.agents.get(args[0])
            if agent:
                agent.write(args[1])
        elif kind == "paste":
            agent = self.agents.get(args[0])
            if agent:
                agent.paste(args[1])
        elif kind == "view":
            client.view, client.offset = args
            client.sent_screen = None
        elif kind == "resize":
            self.size = (max(args[0], 2), max(args[1], 10))
            for agent in list(self.agents.values()):
                agent.resize(*self.size)
        elif kind == "create_space":  # a new space, with a first tab running `command`
            name, cwd, command = args
            base_name = name.strip() or suggested_space_name(cwd)
            with self.lock:
                used_names = {sp["name"] for sp in self.spaces}
                unique_name = base_name
                number = 2
                while unique_name in used_names:
                    unique_name = f"{base_name} ({number})"
                    number += 1
                space = {"id": uuid.uuid4().hex[:12], "name": unique_name, "cwd": cwd}
                self.spaces.append(space)
            try:
                tab_name = command.split()[0] if command.strip() else "shell"
                self._create_agent(client, space, tab_name, cwd, command)
            except Exception:
                with self.lock:
                    self.spaces.remove(space)
                raise
        elif kind == "create":
            space_id, name, cwd, command = args
            space = next((sp for sp in self.spaces if sp["id"] == space_id), None)
            if space is None:
                raise ValueError("that space no longer exists")
            self._create_agent(client, space, name, cwd, command)
        elif kind == "rename":
            agent = self.agents.get(args[0])
            if agent:
                agent.name = args[1]
        elif kind == "rename_space":
            for space in self.spaces:
                if space["id"] == args[0]:
                    space["name"] = args[1]
        elif kind == "close":
            with self.lock:
                agent = self.agents.pop(args[0], None)
            if agent:
                agent.kill()
        elif kind == "close_space":
            with self.lock:
                self.spaces = [sp for sp in self.spaces if sp["id"] != args[0]]
                doomed = [a for a in self.agents.values() if a.space["id"] == args[0]]
                for agent in doomed:
                    del self.agents[agent.id]
            for agent in doomed:
                agent.kill()
        elif kind == "shutdown":
            for agent in list(self.agents.values()):
                agent.kill()
            if os.name != "nt" and os.path.exists(address()):
                os.unlink(address())
            os._exit(0)

    def _create_agent(self, client, space, name, cwd, command):
        agent = Agent(space, name, cwd, command)
        agent.start(*self.size)  # raises (-> error to the client) if the command can't start
        with self.lock:
            self.agents[agent.id] = agent
        client.send(("created", agent.id))

    def _poll_hook_states(self):
        records = {}
        for f in HERD_DIR.glob("*.json"):
            try:
                record = json.loads(f.read_text())
            except Exception:
                continue
            if record.get("herd_id"):
                records[record["herd_id"]] = record
        for agent in list(self.agents.values()):
            record = records.get(agent.id)
            if agent.alive:
                if record:
                    agent.set_state(record["state"])
                elif not agent.command.strip():
                    agent.set_state("idle")

    def _tick_loop(self):
        last_poll = 0
        while True:
            time.sleep(1 / 30)
            if time.time() - last_poll >= 0.5:
                last_poll = time.time()
                self._poll_hook_states()
            with self.lock:
                meta = {"spaces": [dict(sp) for sp in self.spaces],
                        "agents": [a.meta() for a in self.agents.values()]}
                clients = list(self.clients)
            for client in clients:
                if meta != client.sent_meta:
                    client.sent_meta = meta
                    client.send(("state", meta))
                agent = self.agents.get(client.view)
                if agent:
                    key = (agent.id, agent.version, client.offset)
                    if key != client.sent_screen:
                        client.sent_screen = key
                        client.send(("screen", agent.snapshot(client.offset)))


def main():
    existing = try_connect()
    if existing:
        existing.close()
        print("herd server already running", flush=True)
        return
    Server().serve()


if __name__ == "__main__":
    main()

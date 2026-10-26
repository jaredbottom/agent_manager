"""herd server - owns tabs' terminals so they keep running after the window is closed.

Started automatically by `herd app`; stopped with `herd stop` (or prefix then Q in the app).
"""

import base64
import binascii
import os
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
from multiprocessing.connection import Listener
from pathlib import Path

import pyte
from wcwidth import wcwidth, wcswidth

from agent_manager.ipc import address, authkey, try_connect

if os.name == "nt":
    from winpty import PtyProcess
else:  # lets herd run on Linux/macOS too, handy for development
    from ptyprocess import PtyProcessUnicode as PtyProcess

SCROLLBACK = 10000
BRACKETED_PASTE = 2004 << 5  # how pyte stores private mode ?2004
CWD_TITLE_PREFIX = "herd-cwd-v1:"
POWERSHELL_CWD_PROMPT = """
$global:__herd_original_prompt = (Get-Command prompt -CommandType Function).ScriptBlock
function global:prompt {
    $original = & $global:__herd_original_prompt
    try {
        $cwd = $ExecutionContext.SessionState.Path.CurrentFileSystemLocation.Path
        $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($cwd))
        [Console]::Write("$([char]27)]2;herd-cwd-v1:$encoded$([char]7)")
    } catch {}
    return $original
}
"""


def encoded_powershell_prompt():
    return base64.b64encode(POWERSHELL_CWD_PROMPT.encode("utf-16le")).decode("ascii")


def powershell_command(shell):
    return f"{shell} -NoExit -EncodedCommand {encoded_powershell_prompt()}"


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


class TermScreen:
    """Keep full-screen programs off the shell's screen and scrollback."""

    ALTERNATE_MODES = {47, 1047, 1049}

    def __init__(self, columns, lines, reply):
        self.main = pyte.HistoryScreen(columns, lines, history=SCROLLBACK, ratio=0.5)
        self.alternate = pyte.Screen(columns, lines)
        self.active = self.main
        self._reply = reply
        self.main.write_process_input = reply
        self.alternate.write_process_input = reply
        self.cwd = None
        self._join_next = False

    def __getattr__(self, name):
        value = getattr(self.active, name)
        if not callable(value):
            return value

        # pyte caches event handlers when it attaches a screen. Resolve the
        # active buffer when each event arrives, including within one chunk.
        def forward(*args, **kwargs):
            return getattr(self.active, name)(*args, **kwargs)

        return forward

    @property
    def in_alternate(self):
        return self.active is self.alternate

    def write_process_input(self, data):
        self._reply(data)

    def _append_to_previous(self, suffix):
        screen = self.active
        y = screen.cursor.y
        x = screen.cursor.x - 1
        if x < 0:
            return False
        row = screen.buffer[y]
        while x >= 0 and row[x].data == "":
            x -= 1
        if x < 0:
            return False
        previous = row[x]
        combined = previous.data + suffix
        before, after = wcswidth(previous.data), wcswidth(combined)
        if (before < 0 or after < 0
                or (after > before and screen.cursor.x + after - before > screen.columns)):
            return False
        if after > before:
            for cell in range(screen.cursor.x, screen.cursor.x + after - before):
                row[cell] = previous._replace(data="")
            screen.cursor.x += after - before
        elif after < before:
            for cell in range(screen.cursor.x - before + after, screen.cursor.x):
                row.pop(cell, None)
            screen.cursor.x -= before - after
        row[x] = previous._replace(data=combined)
        screen.dirty.add(y)
        return True

    def draw(self, data):
        # pyte 0.8.2 drops everything after a zero-width codepoint. Attach
        # emoji modifiers and combining marks to their existing screen cell.
        ordinary = []

        def flush():
            if ordinary:
                self.active.draw("".join(ordinary))
                ordinary.clear()

        for char in data:
            code = ord(char)
            if char == "\u200d":
                flush()
                self._join_next = self._append_to_previous(char)
            elif ((unicodedata.category(char).startswith("M") and wcwidth(char) == 0)
                  or 0x1F3FB <= code <= 0x1F3FF):
                flush()
                self._append_to_previous(char)
            elif wcwidth(char) <= 0:
                flush()
                self._join_next = False
            elif self._join_next:
                flush()
                if not self._append_to_previous(char):
                    self.active.draw(char)
                self._join_next = False
            else:
                ordinary.append(char)
        flush()

    def save_cursor(self, *_args):
        self.active.save_cursor()

    def restore_cursor(self, *_args):
        self.active.restore_cursor()

    def set_title(self, param):
        if param.startswith(CWD_TITLE_PREFIX):
            try:
                cwd = base64.b64decode(param[len(CWD_TITLE_PREFIX):], validate=True).decode("utf-8")
            except (binascii.Error, UnicodeError):
                return
            if cwd:
                self.cwd = cwd
        else:
            self.active.set_title(param)

    def set_mode(self, *modes, **kwargs):
        if kwargs.get("private") and self.ALTERNATE_MODES.intersection(modes):
            if not self.in_alternate:
                if self.ALTERNATE_MODES.intersection(modes) != {47}:
                    self.alternate.reset()
                self.active = self.alternate
        self.active.set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        self.active.reset_mode(*modes, **kwargs)
        if kwargs.get("private") and self.ALTERNATE_MODES.intersection(modes):
            self.active = self.main

    def reset(self):
        self.main.reset()
        self.alternate.reset()
        self.active = self.main
        self._join_next = False

    def resize(self, lines, columns):
        self.main.resize(lines, columns)
        self.alternate.resize(lines, columns)

    def scroll_up(self, count=1):
        screen = self.active
        top, bottom = screen.margins or (0, screen.lines - 1)
        old_y = screen.cursor.y
        screen.cursor.y = bottom
        for _ in range(min(count or 1, bottom - top + 1)):
            screen.index()
        screen.cursor.y = old_y

    def scroll_down(self, count=1):
        screen = self.active
        top, bottom = screen.margins or (0, screen.lines - 1)
        old_y = screen.cursor.y
        screen.cursor.y = top
        for _ in range(min(count or 1, bottom - top + 1)):
            screen.reverse_index()
        screen.cursor.y = old_y


class TermStream(pyte.Stream):
    """Parse terminal controls used by modern interactive programs."""

    csi = {**pyte.Stream.csi, "S": "scroll_up", "T": "scroll_down",
           "s": "save_cursor", "u": "restore_cursor"}
    events = pyte.Stream.events | {"scroll_up", "scroll_down"}


class Agent:
    def __init__(self, space, name, cwd, command):
        self.id = uuid.uuid4().hex[:12]
        self.space = space  # the space dict this agent's tab lives in
        self.name = name
        self.cwd = cwd
        self.command = command
        self.alive = False
        self.version = 0  # bumped whenever the screen changes
        self.lock = threading.Lock()
        self.screen = TermScreen(80, 24, self.write)
        self.stream = TermStream(self.screen)
        self.proc = None

    def argv(self):
        if not self.command.strip():
            if os.name == "nt":
                if shutil.which("pwsh"):
                    return powershell_command("pwsh")
                if shutil.which("powershell"):
                    return powershell_command("powershell")
                return os.environ.get("COMSPEC", "cmd.exe")
            shell = os.environ.get("SHELL") or "/bin/sh"
            if Path(shell).name in {"pwsh", "powershell"}:
                return [shell, "-NoExit", "-EncodedCommand", encoded_powershell_prompt()]
            return [shell]
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
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
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

    def current_cwd(self):
        with self.lock:
            reported = self.screen.cwd
        if reported and os.path.isdir(reported):
            return reported
        if os.name != "nt" and self.alive and self.proc:
            try:
                path = os.readlink(f"/proc/{self.proc.pid}/cwd")
                if os.path.isdir(path):
                    return path
            except OSError:
                pass
        return self.cwd

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

    def kill(self):
        if self.alive:
            try:
                self.proc.terminate(force=True)
            except Exception:
                pass

    def meta(self):
        return {"id": self.id, "space": self.space["id"], "name": self.name, "cwd": self.cwd,
                "alive": self.alive}

    def snapshot(self, offset):
        """The visible screen (or a scrolled-back view) as rows of (text, style) runs."""
        with self.lock:
            screen = self.screen
            history = screen.main.history.top if not screen.in_alternate else ()
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
        self.auto_named_spaces = set()
        self.followed_agents = {}  # space id -> last viewed shell tab
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
            agent = self.agents.get(client.view)
            if agent and not agent.command.strip():
                self.followed_agents[agent.space["id"]] = agent.id
        elif kind == "resize":
            self.size = (max(args[0], 2), max(args[1], 10))
            for agent in list(self.agents.values()):
                agent.resize(*self.size)
        elif kind == "create_space":  # a new space, with a first tab running `command`
            name, cwd, command, *source_ids = args
            if source_ids:
                source = self.agents.get(source_ids[0])
                if source:
                    cwd = source.current_cwd()
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
                if not name.strip():
                    self.auto_named_spaces.add(space["id"])
            try:
                tab_name = command.split()[0] if command.strip() else "shell"
                self._create_agent(client, space, tab_name, cwd, command)
            except Exception:
                with self.lock:
                    self.spaces.remove(space)
                    self.auto_named_spaces.discard(space["id"])
                raise
        elif kind == "create":
            space_id, name, cwd, command, *source_ids = args
            space = next((sp for sp in self.spaces if sp["id"] == space_id), None)
            if space is None:
                raise ValueError("that space no longer exists")
            if source_ids:
                source = self.agents.get(source_ids[0])
                if source and source.space["id"] == space_id:
                    cwd = source.current_cwd()
            self._create_agent(client, space, name, cwd, command)
        elif kind == "rename":
            agent = self.agents.get(args[0])
            if agent:
                agent.name = args[1]
        elif kind == "rename_space":
            for space in self.spaces:
                if space["id"] == args[0]:
                    space["name"] = args[1]
                    self.auto_named_spaces.discard(space["id"])
        elif kind == "close":
            with self.lock:
                agent = self.agents.pop(args[0], None)
            if agent:
                agent.kill()
        elif kind == "close_space":
            with self.lock:
                self.spaces = [sp for sp in self.spaces if sp["id"] != args[0]]
                self.auto_named_spaces.discard(args[0])
                self.followed_agents.pop(args[0], None)
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

    def _poll_space_locations(self):
        for space in list(self.spaces):
            shells = [agent for agent in self.agents.values()
                      if agent.space["id"] == space["id"] and agent.alive
                      and not agent.command.strip()]
            if not shells:
                continue
            followed = self.followed_agents.get(space["id"])
            agent = next((shell for shell in shells if shell.id == followed), shells[0])
            cwd = agent.current_cwd()
            if cwd == space["cwd"]:
                continue
            base_name = suggested_space_name(cwd) if space["id"] in self.auto_named_spaces else None
            with self.lock:
                if space not in self.spaces:
                    continue
                space["cwd"] = cwd
                if space["id"] in self.auto_named_spaces:
                    used_names = {other["name"] for other in self.spaces if other is not space}
                    unique_name = base_name
                    number = 2
                    while unique_name in used_names:
                        unique_name = f"{base_name} ({number})"
                        number += 1
                    space["name"] = unique_name

    def _tick_loop(self):
        last_poll = 0
        while True:
            time.sleep(1 / 30)
            if time.time() - last_poll >= 0.5:
                last_poll = time.time()
                self._poll_space_locations()
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

"""herd app - one window, a sidebar of agents, and a live terminal for the selected one.

The agents run inside the herd server; this window just attaches to it. Close the window
(prefix, then d) and the agents keep going; run `herd app` again to reattach.
"""

import os
import threading
import time
from pathlib import Path

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Input, Label, Static

from agent_manager.ipc import connect_or_start

STATE_STYLE = {
    "working": ("●", "yellow"),
    "blocked": ("●", "bold red"),
    "done": ("●", "bold green"),
    "idle": ("○", "grey50"),
    "starting": ("○", "grey50"),
    "exited": ("✕", "grey37"),
}
NEEDS_YOU = {"blocked", "done"}

KEYMAP = {
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "home": "\x1b[H", "end": "\x1b[F", "insert": "\x1b[2~", "delete": "\x1b[3~",
    "pageup": "\x1b[5~", "pagedown": "\x1b[6~",
    "enter": "\r", "shift+enter": "\x1b\r", "tab": "\t", "shift+tab": "\x1b[Z",
    "backspace": "\x7f", "escape": "\x1b",
    "ctrl+up": "\x1b[1;5A", "ctrl+down": "\x1b[1;5B",
    "ctrl+right": "\x1b[1;5C", "ctrl+left": "\x1b[1;5D",
    "f1": "\x1bOP", "f11": "\x1b[23~",
}
# ctrl+a .. ctrl+z -> \x01 .. \x1a
KEYMAP.update({f"ctrl+{c}": chr(i + 1) for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")})
KEYMAP.update({"ctrl+@": "\x00", "ctrl+space": "\x00", "ctrl+backslash": "\x1c",
               "ctrl+right_square_bracket": "\x1d"})

# prefix spellings accepted by --prefix -> the key names Textual reports for them
PREFIX_ALIASES = {
    "ctrl+space": {"ctrl+@", "ctrl+space", "ctrl+at"},
    "ctrl+]": {"ctrl+right_square_bracket"},
    "ctrl+\\": {"ctrl+backslash"},
}

# character typed after the prefix -> app action (mirrors herdr's defaults)
PREFIX_COMMANDS = {
    "c": "new_tab", "n": "next_tab", "p": "prev_tab", "T": "rename_tab", "x": "close_tab",
    "C": "new_space", "w": "next_space", "W": "rename_space", "D": "close_space",
    "a": "next_waiting", "[": "scrollback", "d": "detach", "Q": "stop_all", "?": "help",
}
# shortcuts that work without the prefix
DIRECT_KEYS = {
    "alt+up": "prev_space", "alt+down": "next_space",
    "alt+left": "prev_tab", "alt+right": "next_tab",
}
HELP_LINES = [
    "c  new tab            C  new space",
    "n/p  next/prev tab    w  next space",
    "1-9  jump to tab      W  rename space",
    "T  rename tab         D  close space",
    "x  close tab          a  next agent waiting on you",
    "[  scrollback         d  detach (agents keep running)",
    "?  this help          Q  stop herd and all agents",
    "",
    "No prefix needed: Alt+Up/Down switch spaces, Alt+Left/Right switch tabs",
]
STATUS_HINT = "c tab · C space · 1-9 · a waiting · x close · d detach · ? help   Alt+←/→ tabs · Alt+↑/↓ spaces"


# --- rendering -------------------------------------------------------------

def pyte_color(name):
    if name == "default":
        return None
    if len(name) == 6 and all(c in "0123456789abcdefABCDEF" for c in name):
        return "#" + name
    name = name.replace("brown", "yellow")
    if name.startswith("bright"):
        return "bright_" + name[6:]
    return name


_style_cache = {}


def run_style(key):
    """Turn a pyte attribute tuple sent by the server into a Rich style."""
    style = _style_cache.get(key)
    if style is None:
        fg, bg, bold, italic, underline, strike, reverse = key
        try:
            style = Style(color=pyte_color(fg), bgcolor=pyte_color(bg), bold=bold, italic=italic,
                          underline=underline, strike=strike, reverse=reverse)
        except Exception:
            style = Style(reverse=reverse)
        _style_cache[key] = style
    return style


class TerminalView(Widget, can_focus=True):
    """Draws the selected agent's screen and forwards keystrokes to it."""

    DEFAULT_CSS = "TerminalView { width: 1fr; height: 1fr; }"

    def __init__(self):
        super().__init__()
        self.snapshot = None
        self.scroll_lines = 0

    def on_resize(self, event):
        self.app.send("resize", event.size.height, event.size.width)

    def render_line(self, y):
        width = self.size.width
        snap = self.snapshot
        if snap is None:
            if y == 1 and not self.app.spaces:
                hint = f"  No spaces yet. Press {self.app.prefix} then N to create one."
                return Strip([Segment(hint)]).adjust_cell_length(width)
            if y == 1 and self.app.selected_id is None:
                hint = f"  No tabs in this space. Press {self.app.prefix} then c to open one."
                return Strip([Segment(hint)]).adjust_cell_length(width)
            return Strip.blank(width)
        rows = snap["rows"]
        if y >= len(rows):
            return Strip.blank(width)
        segments = [Segment(text, run_style(key)) for text, key in rows[y]]
        if snap["offset"] and y == 0:
            banner = f" ↑ scrollback ({snap['offset']} lines) "
            segments = [Segment(banner, Style(reverse=True))] + segments
        return Strip(segments).adjust_cell_length(width)

    def on_key(self, event):
        event.stop()
        event.prevent_default()
        app = self.app
        if app.prefix_pending:
            app.prefix_pending = False
            app.refresh_status()
            app.run_prefix_command(event)
            return
        if event.key in app.prefix_keys:
            app.prefix_pending = True
            app.refresh_status()
            return
        if event.key in DIRECT_KEYS:
            getattr(app, f"action_{DIRECT_KEYS[event.key]}")()
            return
        agent_id = app.selected_id
        if agent_id is None:
            return
        if event.key in ("shift+pageup", "shift+pagedown"):
            self.scroll_by_lines(self.size.height // 2 * (1 if event.key == "shift+pageup" else -1))
        else:
            data = KEYMAP.get(event.key) or event.character
            if not data:
                return
            if self.scroll_lines:
                self.set_offset(0)
            app.send("input", agent_id, data)

    def on_paste(self, event):
        if self.app.selected_id:
            self.app.send("paste", self.app.selected_id, event.text)
            event.stop()

    def set_offset(self, offset):
        self.scroll_lines = offset
        self.app.send("view", self.app.selected_id, offset)

    def scroll_by_lines(self, lines):
        if self.app.selected_id is None or self.snapshot is None:
            return
        self.set_offset(max(0, min(self.snapshot["history"], self.scroll_lines + lines)))

    def on_mouse_scroll_up(self, event):
        self.scroll_by_lines(3)

    def on_mouse_scroll_down(self, event):
        self.scroll_by_lines(-3)

    def on_click(self, event):
        self.focus()


class Sidebar(Static):
    """Spaces, each with its tabs and their agent states."""

    DEFAULT_CSS = """
    Sidebar { width: 30; height: 1fr; border-right: solid $primary-darken-2; padding: 0 1; }
    """

    def __init__(self):
        super().__init__()
        self.rows = []  # what each rendered line points at: ("space"|"tab", id) or None

    def render_state(self, app):
        out, rows = Text(), []

        def line(parts=(), target=None):
            for text, style in parts:
                out.append(text, style=style)
            out.append("\n")
            rows.append(target)

        line([("spaces", "bold")])
        line()
        for space in app.spaces:
            current = space["id"] == app.space_id
            tabs = app.tabs(space["id"])
            parts = [("▶ " if current else "  ", "bold"), (space["name"][:20], "bold" if current else "")]
            waiting = sum(1 for t in tabs if t["state"] in NEEDS_YOU)
            if waiting:
                parts.append((f" ({waiting})", "bold yellow"))
            line(parts, ("space", space["id"]))
            for i, tab in enumerate(tabs):
                glyph, color = STATE_STYLE.get(tab["state"], ("?", ""))
                selected = tab["id"] == app.selected_id
                label = f"{i + 1} {tab['name']}"[:13]
                state_style = color if tab["state"] in NEEDS_YOU else "grey50"
                line([("  ", ""), (f"{glyph} ", color), (f"{label:<14}", "bold" if selected else ""),
                      (tab["state"], state_style)], ("tab", tab["id"]))
            line()
        if not app.spaces:
            line([("no spaces yet", "grey50")])
        self.rows = rows
        self.update(out)

    def on_click(self, event):
        if 0 <= event.y < len(self.rows) and self.rows[event.y]:
            kind, target = self.rows[event.y]
            if kind == "space":
                self.app.select_space(target)
            else:
                self.app.select(target)


class TabBar(Static):
    """The current space's tabs, one per agent."""

    DEFAULT_CSS = "TabBar { height: 1; background: $panel; }"

    def __init__(self):
        super().__init__()
        self.ranges = []  # (start_x, end_x, agent_id)

    def render_state(self, app):
        out, ranges = Text(), []
        space = app.current_space()
        if space:
            out.append(f" {space['name']} ", style="bold")
            out.append("│")
            for i, tab in enumerate(app.tabs(space["id"])):
                glyph, color = STATE_STYLE.get(tab["state"], ("?", ""))
                bg = " on grey30" if tab["id"] == app.selected_id else ""
                start = out.cell_len
                out.append(f" {i + 1} ", style="bold" + bg)
                out.append(glyph, style=color + bg)
                out.append(f" {tab['name']} ", style=("bold" if bg else "") + bg)
                ranges.append((start, out.cell_len, tab["id"]))
                out.append("│")
        self.ranges = ranges
        self.update(out)

    def on_click(self, event):
        for start, end, agent_id in self.ranges:
            if start <= event.x < end:
                self.app.select(agent_id)


class StatusBar(Static):
    DEFAULT_CSS = "StatusBar { dock: bottom; height: 1; background: $panel; color: $text-muted; }"


# --- dialogs ---------------------------------------------------------------

class FormDialog(ModalScreen):
    """A few text fields; dismisses with {key: value}, or None on Esc. A "dir" field must exist."""

    DEFAULT_CSS = """
    FormDialog { align: center middle; }
    FormDialog > Vertical { width: 70; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    FormDialog Input { margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    def __init__(self, title, fields):
        super().__init__()
        self.heading = title
        self.fields = fields  # [(key, placeholder, initial value)]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"{self.heading}  (Enter to confirm, Esc to cancel)")
            for key, placeholder, value in self.fields:
                yield Input(value=value, placeholder=placeholder, id=f"field-{key}")

    def on_input_submitted(self, _event):
        values = {key: self.query_one(f"#field-{key}", Input).value.strip() for key, _, _ in self.fields}
        if "dir" in values:
            directory = os.path.expanduser(values["dir"] or ".")
            if not os.path.isdir(directory):
                self.notify(f"Not a directory: {directory}", severity="error")
                return
            values["dir"] = os.path.abspath(directory)
        self.dismiss(values)


class ConfirmDialog(ModalScreen):
    DEFAULT_CSS = """
    ConfirmDialog { align: center middle; }
    ConfirmDialog > Label { width: 60; border: thick $error; background: $surface; padding: 1 2; }
    """

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        yield Label(f"{self.message}\n\n[b]y[/b] = yes, any other key = no")

    def on_key(self, event):
        event.stop()
        self.dismiss(event.key == "y")


# --- app -------------------------------------------------------------------

class HerdApp(App):
    TITLE = "herd"
    ENABLE_COMMAND_PALETTE = False  # it would steal ctrl+p from the agents
    CSS = "#main { width: 1fr; }"

    def __init__(self, conn, default_cmd="claude", prefix="ctrl+space"):
        super().__init__()
        self.conn = conn
        self.send_lock = threading.Lock()
        self.default_cmd = default_cmd
        self.prefix = prefix
        self.prefix_keys = PREFIX_ALIASES.get(prefix, {prefix})
        self.prefix_pending = False
        self.spaces = []
        self.agents = []
        self.space_id = None
        self.selected_id = None
        self.last_tab = {}  # space id -> the tab last viewed there
        self.pending_select = None  # tab we created but haven't heard about yet
        self.server_gone = False

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Sidebar()
            with Vertical(id="main"):
                yield TabBar()
                yield TerminalView()
        yield StatusBar()

    def on_mount(self):
        self.terminal = self.query_one(TerminalView)
        self.sidebar = self.query_one(Sidebar)
        self.tabbar = self.query_one(TabBar)
        self.status = self.query_one(StatusBar)
        self.refresh_status()
        self.refresh_ui()
        threading.Thread(target=self._receive_loop, daemon=True).start()
        self.terminal.focus()

    # talking to the server

    def send(self, *message):
        with self.send_lock:
            try:
                self.conn.send(message)
            except Exception:
                pass

    def _receive_loop(self):
        while True:
            try:
                message = self.conn.recv()
            except Exception:
                break
            try:
                self.call_from_thread(self.on_server_message, message)
            except Exception:
                return
        if not self.is_running:  # we detached; the connection closing is expected
            return
        try:
            self.call_from_thread(self.on_server_lost)
        except Exception:
            pass

    def on_server_message(self, message):
        kind, payload = message
        if kind == "state":
            self.update_state(payload)
        elif kind == "screen":
            if payload["id"] == self.selected_id:
                self.terminal.snapshot = payload
                self.terminal.scroll_lines = payload["offset"]
                self.terminal.refresh()
        elif kind == "created":
            if self.find(payload):
                self.select(payload)
            else:
                self.pending_select = payload
        elif kind == "error":
            self.notify(payload, severity="error", timeout=8)

    def on_server_lost(self):
        self.server_gone = True
        self.exit(message="herd server stopped.")

    def update_state(self, state):
        before = {a["id"]: a["state"] for a in self.agents}
        names = {sp["id"]: sp["name"] for sp in state["spaces"]}
        for agent in state["agents"]:
            old = before.get(agent["id"])
            if old and old != agent["state"] and agent["state"] in NEEDS_YOU and agent["id"] != self.selected_id:
                self.bell()
                self.notify(f"{names.get(agent['space'], '?')} / {agent['name']}: {agent['state']}", timeout=4)
        self.spaces, self.agents = state["spaces"], state["agents"]

        if self.pending_select and self.find(self.pending_select):
            self.select(self.pending_select)
            self.pending_select = None
        elif self.space_id not in names:
            self.select_space(self.spaces[0]["id"] if self.spaces else None)
        elif not self.find(self.selected_id):
            self.select_space(self.space_id)
        self.refresh_ui()

    # lookups

    def find(self, agent_id):
        return next((a for a in self.agents if a["id"] == agent_id), None)

    def tabs(self, space_id):
        return [a for a in self.agents if a["space"] == space_id]

    def current_space(self):
        return next((sp for sp in self.spaces if sp["id"] == self.space_id), None)

    # selection

    def refresh_ui(self):
        self.sidebar.render_state(self)
        self.tabbar.render_state(self)

    def select(self, agent_id):
        agent = self.find(agent_id)
        if agent_id != self.selected_id:
            self.terminal.snapshot = None
            self.terminal.scroll_lines = 0
        self.selected_id = agent["id"] if agent else None
        if agent:
            self.space_id = agent["space"]
            self.last_tab[self.space_id] = agent["id"]
        self.send("view", self.selected_id, self.terminal.scroll_lines)
        self.terminal.refresh()
        self.terminal.focus()
        self.refresh_ui()

    def select_space(self, space_id):
        self.space_id = space_id
        ids = [t["id"] for t in self.tabs(space_id)]
        remembered = self.last_tab.get(space_id)
        self.select(remembered if remembered in ids else (ids[0] if ids else None))

    # prefix commands

    def refresh_status(self):
        name = self.prefix.replace("ctrl+", "Ctrl+").replace("space", "Space")
        if self.prefix_pending:
            self.status.update(Text(f" {name} … {STATUS_HINT}", style="bold reverse"))
        else:
            self.status.update(Text(f" {name} then: {STATUS_HINT}"))

    def run_prefix_command(self, event):
        ch = event.character
        if event.key in self.prefix_keys:  # prefix twice sends it to the agent
            if self.selected_id:
                self.send("input", self.selected_id, KEYMAP.get(event.key, ""))
        elif ch and ch in "123456789":
            tabs = self.tabs(self.space_id)
            if int(ch) <= len(tabs):
                self.select(tabs[int(ch) - 1]["id"])
        elif ch and ch in PREFIX_COMMANDS:
            getattr(self, f"action_{PREFIX_COMMANDS[ch]}")()

    def action_help(self):
        self.notify(f"{self.prefix} then:\n" + "\n".join(HELP_LINES), timeout=15)

    # tabs

    def action_new_tab(self):
        space = self.current_space()
        if space is None:
            self.action_new_space()
            return

        def create(values):
            if values:
                command = values["cmd"] or self.default_cmd
                self.send("create", space["id"], values["name"] or command.split()[0], values["dir"], command)

        self.push_screen(FormDialog(f"New tab in '{space['name']}'", [
            ("cmd", "command", self.default_cmd),
            ("name", "tab name (default: the command)", ""),
            ("dir", "working directory", space["cwd"]),
        ]), create)

    def _step_tab(self, delta):
        ids = [t["id"] for t in self.tabs(self.space_id)]
        if ids:
            i = ids.index(self.selected_id) if self.selected_id in ids else 0
            self.select(ids[(i + delta) % len(ids)])

    def action_next_tab(self):
        self._step_tab(1)

    def action_prev_tab(self):
        self._step_tab(-1)

    def action_rename_tab(self):
        tab = self.find(self.selected_id)
        if tab:
            self.push_screen(FormDialog("Rename tab", [("name", "tab name", tab["name"])]),
                             lambda v: v and v["name"] and self.send("rename", tab["id"], v["name"]))

    def action_close_tab(self):
        tab = self.find(self.selected_id)
        if tab is None:
            return

        def close(confirmed):
            if confirmed:
                self.send("close", tab["id"])

        if tab["state"] == "exited":
            close(True)
        else:
            self.push_screen(ConfirmDialog(f"Kill the agent in tab '{tab['name']}'?"), close)

    def action_scrollback(self):
        self.terminal.scroll_by_lines(self.terminal.size.height // 2)

    # spaces

    def action_new_space(self):
        space = self.current_space()

        def create(values):
            if values:
                name = values["name"] or Path(values["dir"]).name
                self.send("create_space", name, values["dir"], values["cmd"] or self.default_cmd)

        self.push_screen(FormDialog("New space", [
            ("dir", "project folder", space["cwd"] if space else os.getcwd()),
            ("name", "space name (default: folder name)", ""),
            ("cmd", "command for the first tab", self.default_cmd),
        ]), create)

    def _step_space(self, delta):
        ids = [sp["id"] for sp in self.spaces]
        if ids:
            i = ids.index(self.space_id) if self.space_id in ids else 0
            self.select_space(ids[(i + delta) % len(ids)])

    def action_next_space(self):
        self._step_space(1)

    def action_prev_space(self):
        self._step_space(-1)

    def action_rename_space(self):
        space = self.current_space()
        if space:
            self.push_screen(FormDialog("Rename space", [("name", "space name", space["name"])]),
                             lambda v: v and v["name"] and self.send("rename_space", space["id"], v["name"]))

    def action_close_space(self):
        space = self.current_space()
        if space is None:
            return
        count = sum(1 for t in self.tabs(space["id"]) if t["state"] != "exited")

        def close(confirmed):
            if confirmed:
                self.send("close_space", space["id"])

        if count:
            self.push_screen(ConfirmDialog(f"Close space '{space['name']}' and kill its {count} agent(s)?"), close)
        else:
            close(True)

    def action_next_waiting(self):
        ordered = [t for sp in self.spaces for t in self.tabs(sp["id"])]
        ids = [t["id"] for t in ordered]
        start = ids.index(self.selected_id) if self.selected_id in ids else -1
        for step in range(1, len(ordered) + 1):
            tab = ordered[(start + step) % len(ordered)]
            if tab["state"] in NEEDS_YOU:
                self.select(tab["id"])
                return
        self.notify("Nobody is waiting on you.")

    # leaving

    def action_detach(self):
        self.exit()

    def action_quit(self):
        # Textual binds ctrl+q to quit with top priority; hand it to the agent instead
        if self.selected_id and self.focused is self.terminal:
            self.send("input", self.selected_id, "\x11")

    def action_stop_all(self):
        def stop(confirmed):
            if confirmed:
                self.send("shutdown")
                self.server_gone = True
                self.exit(message="Stopped herd and all agents.")

        self.push_screen(ConfirmDialog("Stop herd? This kills ALL agents in every space."), stop)


def run(default_cmd="claude", prefix="ctrl+space"):
    conn = connect_or_start()
    app = HerdApp(conn, default_cmd, prefix)
    app.run()
    conn.close()
    if not app.server_gone and app.agents:
        print(f"Detached - {len(app.agents)} agent(s) still running. `herd app` to reattach.")


class KeyLogApp(App):
    """`herd keys`: shows what Textual receives for each keypress, to debug shortcuts."""

    ENABLE_COMMAND_PALETTE = False

    def compose(self) -> ComposeResult:
        from textual.widgets import RichLog

        yield Label(" Press keys to see what herd receives. Ctrl+Q quits.")
        yield RichLog()

    def on_key(self, event):
        from textual.widgets import RichLog

        event.stop()
        event.prevent_default()
        self.query_one(RichLog).write(f"key={event.key!r}  character={event.character!r}  aliases={event.aliases}")


def keys():
    KeyLogApp().run()

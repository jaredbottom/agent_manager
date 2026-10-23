"""herd - a small agent herder for Claude Code on Windows.

Usage:
    herd install            add status hooks to ~/.claude/settings.json
    herd app [--prefix KEY] open (or reattach to) the herd window
    herd stop               stop the background server and all its agents
    herd ls                 print agent states once
    herd check-keys         inspect shortcut input in a temporary window
    herd clear              forget all recorded sessions
    herd hook               (called by Claude Code hooks, reads JSON on stdin)
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

HERD_DIR = Path.home() / ".herd" / "sessions"
SETTINGS = Path.home() / ".claude" / "settings.json"

# hook event -> state shown in the dashboard
EVENT_STATE = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "Stop": "done",
    "Notification": "blocked",
}
HOOK_EVENTS = list(EVENT_STATE) + ["SessionEnd"]

STATE_STYLE = {
    "working": "\033[33m",  # yellow
    "blocked": "\033[1;31m",  # bold red
    "done": "\033[1;32m",  # bold green
    "idle": "\033[90m",  # grey
}
RESET = "\033[0m"


# --- hook ------------------------------------------------------------------

def cmd_hook(_args):
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    event = data.get("hook_event_name", "")
    session_id = data.get("session_id") or "unknown"
    path = HERD_DIR / f"{session_id}.json"

    if event == "SessionEnd":
        path.unlink(missing_ok=True)
        return
    if event not in EVENT_STATE:
        return

    state = EVENT_STATE[event]
    message = data.get("message", "")
    # "Claude is waiting for your input" fires after Stop when idle; it's not a permission prompt
    if event == "Notification" and "waiting for your input" in message.lower():
        state = "done"

    cwd = data.get("cwd") or os.getcwd()
    record = {
        "name": os.environ.get("HERD_NAME") or Path(cwd).name,
        "herd_id": os.environ.get("HERD_ID", ""),
        "cwd": cwd,
        "state": state,
        "detail": message or (data.get("tool_name") or ""),
        "updated": time.time(),
    }
    HERD_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record))
    os.replace(tmp, path)


# --- install ---------------------------------------------------------------

def hook_command():
    exe = sys.executable.replace("\\", "/")
    return f'"{exe}" -m agent_manager.cli hook'


def is_herd_hook(command):
    return command.endswith("agent_manager.cli hook") or command.endswith('herd.py" hook')


def cmd_install(_args):
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    if SETTINGS.exists():
        shutil.copy(SETTINGS, SETTINGS.with_suffix(".json.herd-backup"))

    command = hook_command()
    hooks = settings.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        # drop hooks from earlier installs (e.g. a different venv), then add ours
        entries = [
            e for e in hooks.get(event, [])
            if not any(is_herd_hook(h.get("command", "")) for h in e.get("hooks", []))
        ]
        hooks[event] = entries
        entry = {"hooks": [{"type": "command", "command": command}]}
        if event in ("PreToolUse", "PostToolUse"):
            entry["matcher"] = "*"
        entries.append(entry)

    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2))
    print(f"Hooks installed in {SETTINGS}")
    print("Restart any running claude sessions to pick them up.")


# --- listing -------------------------------------------------------------

def load_sessions():
    sessions = []
    for f in HERD_DIR.glob("*.json"):
        try:
            sessions.append(json.loads(f.read_text()))
        except Exception:
            pass
    order = {"blocked": 0, "done": 1, "working": 2, "idle": 3}
    sessions.sort(key=lambda s: (order.get(s["state"], 9), s["name"]))
    return sessions


def ago(ts):
    secs = int(time.time() - ts)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h"


def render(sessions):
    lines = [f"{'AGENT':<20} {'STATE':<9} {'FOR':>4}  DETAIL", "-" * 72]
    for s in sessions:
        style = STATE_STYLE.get(s["state"], "")
        detail = (s.get("detail") or "")[:36]
        lines.append(
            f"{s['name'][:20]:<20} {style}{s['state']:<9}{RESET} {ago(s['updated']):>4}  {detail}"
        )
    if not sessions:
        lines.append("(no agents yet - start some with: herd app)")
    return "\n".join(lines)


def cmd_ls(_args):
    if os.name == "nt":
        os.system("")  # enable ANSI colours in the Windows console
    print(render(load_sessions()))


def cmd_app(args):
    from agent_manager import app  # imported lazily so the hook stays fast

    app.run(args.cmd, args.prefix)


def cmd_check_keys(_args):
    from agent_manager import app

    app.check_keys()


def cmd_stop(_args):
    from agent_manager.ipc import try_connect

    conn = try_connect()
    if conn is None:
        print("herd server isn't running.")
        return
    conn.send(("shutdown",))
    conn.close()
    print("Stopped herd and all agents.")


def cmd_server(_args):
    from agent_manager import server

    server.main()


def cmd_clear(_args):
    for f in HERD_DIR.glob("*.json"):
        f.unlink(missing_ok=True)
    print("Cleared.")


def main():
    parser = argparse.ArgumentParser(prog="herd", description="Herd your Claude Code agents.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("hook").set_defaults(func=cmd_hook)
    sub.add_parser("install").set_defaults(func=cmd_install)
    sub.add_parser("ls").set_defaults(func=cmd_ls)
    sub.add_parser("clear").set_defaults(func=cmd_clear)
    sub.add_parser("stop").set_defaults(func=cmd_stop)
    sub.add_parser("server", help="run the server in the foreground (for debugging)").set_defaults(func=cmd_server)

    sub.add_parser("check-keys", help="inspect shortcut input in a temporary window").set_defaults(func=cmd_check_keys)

    p = sub.add_parser("app")
    p.add_argument("--cmd", default="", help="startup command for new tabs (default: an interactive shell)")
    p.add_argument("--prefix", default="ctrl+space", help="prefix key, e.g. ctrl+space, 'ctrl+]', ctrl+b")
    p.set_defaults(func=cmd_app)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

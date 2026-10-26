"""Command line entry point for the herd terminal workspace."""

import argparse
import json
import os
import tempfile
from pathlib import Path


CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


def is_herd_hook(command):
    """Recognize commands installed by older versions of herd."""
    return command.endswith("agent_manager.cli hook") or command.endswith('herd.py" hook')


def cmd_remove_hooks(_args):
    """Remove only herd's old status hooks from Claude settings."""
    if not CLAUDE_SETTINGS.exists():
        print("No Claude settings file found; no herd hooks to remove.")
        return

    settings = json.loads(CLAUDE_SETTINGS.read_text())
    hooks = settings.get("hooks", {})
    removed = 0
    for event, entries in list(hooks.items()):
        kept_entries = []
        for entry in entries:
            old_hooks = entry.get("hooks", [])
            kept_hooks = [hook for hook in old_hooks if not is_herd_hook(hook.get("command", ""))]
            removed += len(old_hooks) - len(kept_hooks)
            if kept_hooks:
                kept_entries.append({**entry, "hooks": kept_hooks})
        if kept_entries:
            hooks[event] = kept_entries
        else:
            del hooks[event]

    if not removed:
        print("No herd hooks found.")
        return
    if not hooks:
        settings.pop("hooks", None)
    with tempfile.NamedTemporaryFile("w", dir=CLAUDE_SETTINGS.parent, prefix=".herd-",
                                     suffix=".tmp", delete=False) as temporary:
        temporary.write(json.dumps(settings, indent=2) + "\n")
    try:
        os.replace(temporary.name, CLAUDE_SETTINGS)
    finally:
        Path(temporary.name).unlink(missing_ok=True)
    print(f"Removed {removed} herd hook(s) from {CLAUDE_SETTINGS}.")


def cmd_app(args):
    from agent_manager import app

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
    print("Stopped herd and all tabs.")


def cmd_server(_args):
    from agent_manager import server

    server.main()


def cmd_old_hook(_args):
    # Previously installed Claude hooks may still invoke this until removed.
    pass


def main():
    parser = argparse.ArgumentParser(prog="herd", description="Terminal spaces and tabs for live sessions.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stop", help="stop the server and its tabs").set_defaults(func=cmd_stop)
    sub.add_parser("server", help="run the server in the foreground (for debugging)").set_defaults(func=cmd_server)
    sub.add_parser("check-keys", help="inspect shortcut input in a temporary window").set_defaults(func=cmd_check_keys)
    sub.add_parser("remove-hooks", help="remove herd's old Claude status hooks").set_defaults(func=cmd_remove_hooks)
    sub.add_parser("hook", help="legacy no-op; remove old hooks with remove-hooks").set_defaults(func=cmd_old_hook)

    app = sub.add_parser("app", help="open or reattach to the workspace")
    app.add_argument("--cmd", default="", help="startup command for new tabs (default: an interactive shell)")
    app.add_argument("--prefix", default="ctrl+space", help="prefix key, e.g. ctrl+space, 'ctrl+]', ctrl+b")
    app.set_defaults(func=cmd_app)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

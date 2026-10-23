# herd

A small, herdr-style terminal workspace on Windows: one window, a sidebar showing
every tab's state, and a live terminal for whichever tab is selected. New tabs
open an interactive shell, so you can run Claude Code or any other command yourself.

## Setup

Needs [uv](https://docs.astral.sh/uv/). From this folder:

```
uv tool install .      # puts a `herd` command on your PATH (re-run with --reinstall after changes)
herd app
```

Or without installing: `uv run herd app`.

After an update, finish any running tabs and use `herd stop` before reopening the
app. The background server keeps its old code until it stops, and `herd stop`
kills its tabs.

Optional: `herd install` adds Claude Code status hooks to
`~/.claude/settings.json` (backed up first), so Claude tabs show when they are
working, blocked, or done. The terminal workspace works without those hooks.

`herd install` points the hooks at whichever environment you ran it from, so run it again if
you switch between `uv tool install` and `uv run` (it replaces its old hooks).

In `herd app`, press Ctrl+Space then Shift+C to create a space immediately. The
first space uses the folder where you launched herd; later spaces use the selected
space's folder. Press Ctrl+Space then `c` to open another shell tab immediately.
To choose a different folder, press Ctrl+Space then Shift+F. On Windows, herd
uses PowerShell (or `pwsh` if installed); on Linux/macOS, it uses `$SHELL`.

Spaces are named for the Git branch when it is neither `main` nor `master`.
On `main`, `master`, or a detached HEAD, they use the repository folder's name.
Outside Git, or if Git is unavailable, they use the working folder's name (or
`workspace` for a filesystem root). Repeated names get a number, such as
`project (2)`. You can rename a space with Shift+W.

If you prefer a startup command, use `herd app --cmd claude`, or enter one in
the Shift+F form. Leave the command field blank to open a shell.

Use Windows Terminal for the best results; the old console host works but looks worse.

## Spaces and tabs

Like herdr: a **space** is usually one project (a folder), and holds **tabs**, each running
one shell or command full screen. The sidebar lists every space with its tabs and their states;
the bar above the terminal shows the current space's tabs. Click either to switch.

## Keys

Everything goes through a prefix, like tmux/herdr: press **Ctrl+Space**, then:

| Key | Action | | Key | Action |
|---|---|---|---|---|
| c | New tab in this space | | C | New space here |
| F | New space in another folder | | W | Rename space |
| n / p | Next / previous tab | | w | Next space |
| 1-9 | Jump to tab N | | T | Rename tab |
| D | Close space (kills its agents) | | x | Close tab |
| a | Jump to the next agent waiting on you, in any space | | ? | Help |
| [ | Scroll back (also Shift+PgUp/PgDn, wheel) | | d | Detach: close the window, agents keep running |
| Q | Stop herd and kill all agents | | | |

**Without the prefix:** Alt+Left/Right switch tabs, Alt+Up/Down switch spaces.

Press the prefix twice to send it to the agent. Every other key goes straight to the agent.
To use a different prefix: `herd app --prefix "ctrl+]"` (or `ctrl+b`, `ctrl+\`, ...).

If Ctrl+Space does nothing in Windows Terminal, run `herd keys` and press it once.
If no key line appears, the terminal is not sending that combination to herd. Try
`herd app --prefix ctrl+b`, or update Windows Terminal. Press Ctrl+Q to leave
`herd keys`. The **C** command is Shift+C; lowercase **c** opens a tab.

## States

Shell tabs show **idle** until they exit. With Claude Code hooks installed, tabs also show
**working**, **blocked** (needs permission), and **done** (finished, waiting for you).
herd beeps when an agent you aren't looking at becomes blocked or done.

## Detach / reattach

The agents run in a hidden background server, started automatically the first time you
run `herd app`. Closing the window (prefix then d, or just closing the terminal) leaves them
running; `herd app` reattaches. `herd stop` shuts everything down.
Server output goes to `~/.herd/server.log`.

## Limitations

- Agents don't survive a reboot or logoff.
- Detailed states only work for Claude Code; other commands run fine but show as "starting".
- Terminal emulation is pyte, so occasional rendering glitches are possible.

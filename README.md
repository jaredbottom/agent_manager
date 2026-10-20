# herd

A small, herdr-style workspace for Claude Code on Windows: one window, a sidebar showing
every agent's state, and a live terminal for whichever agent is selected.

## Setup

Needs [uv](https://docs.astral.sh/uv/). From this folder:

```
uv tool install .      # puts a `herd` command on your PATH (re-run with --reinstall after changes)
herd install           # adds status hooks to ~/.claude/settings.json (backed up first)
herd app
```

Or without installing: `uv run herd install`, then `uv run herd app`.

`herd install` points the hooks at whichever environment you ran it from, so run it again if
you switch between `uv tool install` and `uv run` (it replaces its old hooks).

Use Windows Terminal for the best results; the old console host works but looks worse.

## Spaces and tabs

Like herdr: a **space** is usually one project (a folder), and holds **tabs**, each running
one agent full screen. The sidebar lists every space with its tabs and their states; the
bar above the terminal shows the current space's tabs. Click either to switch.

## Keys

Everything goes through a prefix, like tmux/herdr: press **Ctrl+Space**, then:

| Key | Action | | Key | Action |
|---|---|---|---|---|
| c | New tab in this space | | C | New space |
| n / p | Next / previous tab | | w | Next space |
| 1-9 | Jump to tab N | | W | Rename space |
| T | Rename tab | | D | Close space (kills its agents) |
| x | Close tab | | a | Jump to the next agent waiting on you, in any space |
| [ | Scroll back (also Shift+PgUp/PgDn, wheel) | | d | Detach: close the window, agents keep running |
| ? | Help | | Q | Stop herd and kill all agents |

**Without the prefix:** Alt+Left/Right switch tabs, Alt+Up/Down switch spaces.

Press the prefix twice to send it to the agent. Every other key goes straight to the agent.
To use a different prefix: `herd app --prefix "ctrl+]"` (or `ctrl+b`, `ctrl+\`, ...).

## States

Reported by Claude Code hooks: **working**, **blocked** (needs permission), **done**
(finished, waiting for you), **idle**, **exited**. herd beeps when an agent you aren't
looking at becomes blocked or done.

## Detach / reattach

The agents run in a hidden background server, started automatically the first time you
run `herd app`. Closing the window (prefix then d, or just closing the terminal) leaves them
running; `herd app` reattaches. `herd stop` shuts everything down.
Server output goes to `~/.herd/server.log`.

## Limitations

- Agents don't survive a reboot or logoff.
- States only work for Claude Code; other commands run fine but show as "starting".
- Terminal emulation is pyte, so occasional rendering glitches are possible.

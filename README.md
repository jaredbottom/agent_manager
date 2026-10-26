# herd

A small, herdr-style terminal workspace on Windows: one window, spaces and tabs,
and a live terminal for whichever tab is selected. New tabs
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

If you used `herd install` with an older version, run `herd remove-hooks` once
to remove herd's entries from Claude Code's settings. New installations do not
need hooks.

In `herd app`, press Ctrl+Space then Shift+C to create a space immediately. The
first space uses the folder where you launched herd; later spaces follow the
selected shell's current folder, including after `cd`. Press Ctrl+Space then `c`
to open another shell tab in that folder immediately.
To choose a different folder, press Ctrl+Space then Shift+F. On Windows, herd
uses PowerShell (or `pwsh` if installed); on Linux/macOS, it uses `$SHELL`.
If herd cannot read a shell's current folder, it uses the space's starting folder.

Spaces are named for the Git branch when it is neither `main` nor `master`.
On `main`, `master`, or a detached HEAD, they use the repository folder's name.
Outside Git, or if Git is unavailable, they use the working folder's name (or
`workspace` for a filesystem root). Repeated names get a number, such as
`project (2)`. An automatically named space follows the selected shell when you
`cd` into another folder or repository. Its name and folder update together.
Use Shift+W to set a name that stays fixed.

If you prefer a startup command, use `herd app --cmd claude`, or enter one in
the Shift+F form. Leave the command field blank to open a shell.

Use Windows Terminal for the best results; the old console host works but looks worse.

## Spaces and tabs

Like herdr: a **space** is usually one project (a folder), and holds **tabs**, each running
one shell or command full screen. The sidebar lists every space with its tabs;
the bar above the terminal shows the current space's tabs. Click either to switch.

## Keys

Everything goes through a prefix, like tmux/herdr: press **Ctrl+Space**, then:

| Key | Action | | Key | Action |
|---|---|---|---|---|
| c | New tab in this space | | C | New space here |
| F | New space in another folder | | W | Rename space |
| n / p | Next / previous tab | | w | Next space |
| 1-9 | Jump to tab N | | T | Rename tab |
| D | Close space (kills its tabs) | | x | Close tab |
| [ | Scroll back (also Shift+PgUp/PgDn, wheel) | | ? | Help |
| d | Detach: close the window, tabs keep running | | Q | Stop herd and kill all tabs |

**Without the prefix:** Alt+Left/Right switch tabs, Alt+Up/Down switch spaces.

Press the prefix twice to send it to the selected tab. Every other key goes straight to that tab.
To use a different prefix: `herd app --prefix "ctrl+]"` (or `ctrl+b`, `ctrl+\`, ...).

If Ctrl+Space does nothing in Windows Terminal, run `herd check-keys` and press it once.
If no key line appears, the terminal is not sending that combination to herd. Try
`herd app --prefix ctrl+b`, or update Windows Terminal. Press Ctrl+Q to leave
`herd check-keys`. This check displays input only in its own window and does not
save it. The **C** command is Shift+C; lowercase **c** opens a tab.

Exited tabs show a ✕. They remain visible until you close them.

## Detach / reattach

The tabs run in a hidden background server, started automatically the first time you
run `herd app`. Closing the window (prefix then d, or just closing the terminal) leaves them
running; `herd app` reattaches. `herd stop` shuts everything down.
Server output goes to `~/.herd/server.log`.

## Limitations

- Tabs don't survive a reboot or logoff.
- Terminal emulation is pyte, so occasional rendering glitches are possible.

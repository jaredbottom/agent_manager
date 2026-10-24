"""The default workspace should open a usable shell without a command."""

import base64
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from multiprocessing import Pipe
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rich.cells import cell_len

from agent_manager import server
from agent_manager.app import FormDialog, HerdApp


class ShellTests(unittest.TestCase):
    def test_unicode_format_chars_do_not_drop_following_output(self):
        agent = server.Agent({"id": "s", "name": "s", "cwd": "/"}, "shell", "/", "")
        for output in ("before✳️after", "before👩‍💻after", "before\u200bafter"):
            agent.screen.reset()
            agent.stream.feed(output)
            visible = "".join(run[0] for run in agent.snapshot(0)["rows"][0])
            self.assertIn("before", visible)
            self.assertIn("after", visible)
            self.assertIn(output.replace("\u200b", ""), visible)

        agent.screen.reset()
        agent.stream.feed("before✳")
        agent.stream.feed("️after")
        visible = "".join(run[0] for run in agent.snapshot(0)["rows"][0])
        self.assertIn("before✳️after", visible)
        self.assertEqual(agent.screen.cursor.x, 13)
        self.assertEqual(cell_len(visible.rstrip()), agent.screen.cursor.x)

        agent.screen.reset()
        for part in ("before👩", "\u200d", "💻after"):
            agent.stream.feed(part)
        visible = "".join(run[0] for run in agent.snapshot(0)["rows"][0])
        self.assertIn("before👩‍💻after", visible)
        self.assertEqual(cell_len(visible.rstrip()), agent.screen.cursor.x)

    def test_ansi_cursor_save_and_restore(self):
        agent = server.Agent({"id": "s", "name": "s", "cwd": "/"}, "shell", "/", "")
        agent.stream.feed("abcd\x1b[s\x1b[HXX\x1b[uY")
        visible = "".join(run[0] for run in agent.snapshot(0)["rows"][0])
        self.assertTrue(visible.startswith("XXcdY"))

    def test_full_screen_program_restores_shell_screen(self):
        agent = server.Agent({"id": "s", "name": "s", "cwd": "/"}, "shell", "/", "")
        agent.stream.feed("shell prompt")
        agent.stream.feed("\x1b[?1049h\x1b[Hfull screen")
        alternate = "".join(run[0] for run in agent.snapshot(0)["rows"][0])
        self.assertIn("full screen", alternate)
        self.assertNotIn("shell prompt", alternate)

        agent.stream.feed("\x1b[?1049l")
        restored = "".join(run[0] for run in agent.snapshot(0)["rows"][0])
        self.assertIn("shell prompt", restored)
        self.assertNotIn("full screen", restored)

    def test_alternate_screen_does_not_pollute_scrollback(self):
        agent = server.Agent({"id": "s", "name": "s", "cwd": "/"}, "shell", "/", "")
        agent.stream.feed("\x1b[?1049h")
        for index in range(30):
            agent.stream.feed(f"full-screen line {index}\r\n")
        self.assertEqual(agent.snapshot(100)["history"], 0)
        agent.stream.feed("\x1b[?1049l")
        self.assertEqual(agent.snapshot(100)["history"], 0)

    def test_scroll_up_control_moves_old_output_off_screen(self):
        agent = server.Agent({"id": "s", "name": "s", "cwd": "/"}, "shell", "/", "")
        agent.stream.feed("old output")
        agent.stream.feed("\x1b[24S\x1b[Hnew prompt")
        visible = "".join(run[0] for row in agent.snapshot(0)["rows"] for run in row)
        self.assertIn("new prompt", visible)
        self.assertNotIn("old output", visible)
        self.assertGreater(agent.snapshot(0)["history"], 0)

    @unittest.skipIf(os.name == "nt", "uses a POSIX shell")
    def test_blank_command_starts_interactive_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            space = {"id": "space", "name": "project", "cwd": directory}
            agent = server.Agent(space, "shell", directory, "")
            with patch.dict(os.environ, {"SHELL": "/bin/sh"}):
                agent.start(24, 80)
            try:
                self.assertEqual(agent.state, "idle")
                agent.write("printf SHELL_READY > marker\n")
                marker = Path(directory) / "marker"
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not marker.exists():
                    time.sleep(0.02)
                self.assertEqual(marker.read_text(), "SHELL_READY")
            finally:
                agent.kill()

    def test_windows_shell_selection(self):
        agent = server.Agent({"id": "s", "name": "s", "cwd": "C:/"}, "shell", "C:/", "")
        windows = SimpleNamespace(name="nt", environ={"COMSPEC": "cmd.exe"})
        with patch.object(server, "os", windows):
            with patch.object(server.shutil, "which", side_effect=lambda name: name if name == "pwsh" else None):
                command = agent.argv()
                self.assertTrue(command.startswith("pwsh -NoExit -EncodedCommand "))
                encoded = command.split()[-1]
                self.assertIn("function global:prompt", base64.b64decode(encoded).decode("utf-16le"))
            with patch.object(server.shutil, "which", side_effect=lambda name: name if name == "powershell" else None):
                self.assertTrue(agent.argv().startswith("powershell -NoExit -EncodedCommand "))
            with patch.object(server.shutil, "which", return_value=None):
                self.assertEqual(agent.argv(), "cmd.exe")

    def test_shell_reports_cwd_through_terminal_output(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = server.Agent({"id": "s", "name": "s", "cwd": "/"}, "shell", "/", "")
            encoded = base64.b64encode(directory.encode()).decode()
            sequence = f"\x1b]2;{server.CWD_TITLE_PREFIX}{encoded}\x07"
            agent.stream.feed(sequence[:12])
            agent.stream.feed(sequence[12:])
            self.assertEqual(agent.current_cwd(), directory)

    @unittest.skipIf(os.name == "nt", "uses /proc to read a POSIX shell")
    @unittest.skipUnless(shutil.which("git"), "Git is needed for repository naming")
    def test_current_cwd_follows_cd(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "other-repo"
            target.mkdir()
            subprocess.run(["git", "-C", str(target), "init", "-q", "-b", "feature/other"], check=True)
            space = {"id": "space", "name": "project", "cwd": directory}
            agent = server.Agent(space, "shell", directory, "")
            with patch.dict(os.environ, {"SHELL": "/bin/sh"}):
                agent.start(24, 80)
            try:
                agent.write("cd other-repo\n")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and agent.current_cwd() != str(target):
                    time.sleep(0.02)
                self.assertEqual(agent.current_cwd(), str(target))

                manager = server.Server()
                manager.spaces.append(space)
                manager.agents[agent.id] = agent
                with patch.object(manager, "_create_agent") as create:
                    manager.handle(None, ("create_space", "", directory, "", agent.id))
                    manager.handle(None, ("create", "space", "shell", directory, "", agent.id))
                self.assertEqual(manager.spaces[1]["cwd"], str(target))
                self.assertEqual(manager.spaces[1]["name"], "feature/other")
                self.assertEqual(create.call_args.args[3], str(target))
            finally:
                agent.kill()

    def test_shell_returns_to_idle_after_claude_session_ends(self):
        agent = server.Agent({"id": "s", "name": "s", "cwd": "."}, "shell", ".", "")
        agent.alive = True
        agent.set_state("done")
        manager = server.Server()
        manager.agents[agent.id] = agent
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(server, "HERD_DIR", Path(directory)):
                manager._poll_hook_states()
        self.assertEqual(agent.state, "idle")

    def test_server_creates_first_shell_without_command(self):
        manager = server.Server()
        with patch.object(manager, "_create_agent") as create:
            manager.handle(None, ("create_space", "project", "/tmp", ""))
        self.assertEqual(len(manager.spaces), 1)
        self.assertEqual(create.call_args.args[2:], ("shell", "/tmp", ""))


class SpaceNameTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "Git is needed for repository naming")
    def test_existing_space_follows_shell_until_manually_renamed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "new-repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "feature/new"], check=True)

            manager = server.Server()
            with patch.object(manager, "_create_agent"):
                manager.handle(None, ("create_space", "", directory, ""))
            space = manager.spaces[0]
            shell = server.Agent(space, "shell", directory, "")
            shell.alive = True
            manager.agents[shell.id] = shell

            with patch.object(shell, "current_cwd", return_value=str(repo)):
                manager._poll_space_locations()
            self.assertEqual(space["cwd"], str(repo))
            self.assertEqual(space["name"], "feature/new")

            manager.handle(None, ("rename_space", space["id"], "Pinned"))
            with patch.object(shell, "current_cwd", return_value=directory):
                manager._poll_space_locations()
            self.assertEqual(space["cwd"], directory)
            self.assertEqual(space["name"], "Pinned")

    @unittest.skipUnless(shutil.which("git"), "Git is needed for repository naming")
    def test_branch_repo_and_folder_names(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "my-project"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
            nested = repo / "nested"
            nested.mkdir()
            self.assertEqual(server.suggested_space_name(str(nested)), "my-project")
            subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/feature/login"], check=True)
            self.assertEqual(server.suggested_space_name(str(nested)), "feature/login")
            subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/master"], check=True)
            self.assertEqual(server.suggested_space_name(str(nested)), "my-project")
            self.assertEqual(server.suggested_space_name(directory), Path(directory).name)

    def test_duplicate_names_get_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = server.Server()
            with patch.object(manager, "_create_agent"):
                manager.handle(None, ("create_space", "", directory, ""))
                manager.handle(None, ("create_space", "", directory, ""))
            base = Path(directory).name
            self.assertEqual([space["name"] for space in manager.spaces], [base, f"{base} (2)"])

    def test_fallback_when_git_is_unavailable(self):
        with patch.object(server.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(server.suggested_space_name("/"), "workspace")


class NewTabTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_tab_opens_shell_without_form(self):
        front, back = Pipe()
        app = HerdApp(front)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                app.on_server_message(("state", {
                    "spaces": [{"id": "space", "name": "project", "cwd": "/tmp"}],
                    "agents": [{"id": "agent", "space": "space", "name": "shell",
                                "cwd": "/tmp", "state": "idle", "since": 0}],
                }))
                await pilot.press("ctrl+space", "c")
                await pilot.pause()
                messages = []
                while back.poll():
                    messages.append(back.recv())
                self.assertIn(("create", "space", "shell", "/tmp", "", "agent"), messages)
                self.assertNotIsInstance(app.screen, FormDialog)
                await pilot.press("ctrl+space", "C")
                await pilot.pause()
                messages = []
                while back.poll():
                    messages.append(back.recv())
                self.assertIn(("create_space", "", "/tmp", "", "agent"), messages)
        finally:
            front.close()
            back.close()

    async def test_new_space_opens_immediately(self):
        front, back = Pipe()
        app = HerdApp(front)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.press("ctrl+space", "C")
                await pilot.pause()
                messages = []
                while back.poll():
                    messages.append(back.recv())
                self.assertIn(("create_space", "", os.getcwd(), "", None), messages)
                self.assertNotIsInstance(app.screen, FormDialog)
                await pilot.press("ctrl+space", "F")
                self.assertIsInstance(app.screen, FormDialog)
        finally:
            front.close()
            back.close()


if __name__ == "__main__":
    unittest.main()

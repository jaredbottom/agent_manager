"""The default workspace should open a usable shell without a command."""

import os
import tempfile
import time
import unittest
from multiprocessing import Pipe
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_manager import server
from agent_manager.app import FormDialog, HerdApp


class ShellTests(unittest.TestCase):
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
                self.assertEqual(agent.argv(), "pwsh")
            with patch.object(server.shutil, "which", side_effect=lambda name: name if name == "powershell" else None):
                self.assertEqual(agent.argv(), "powershell")
            with patch.object(server.shutil, "which", return_value=None):
                self.assertEqual(agent.argv(), "cmd.exe")

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


class NewTabTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_tab_opens_shell_without_form(self):
        front, back = Pipe()
        app = HerdApp(front)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                app.on_server_message(("state", {
                    "spaces": [{"id": "space", "name": "project", "cwd": "/tmp"}],
                    "agents": [],
                }))
                await pilot.press("ctrl+space", "c")
                await pilot.pause()
                messages = []
                while back.poll():
                    messages.append(back.recv())
                self.assertIn(("create", "space", "shell", "/tmp", ""), messages)
                self.assertNotIsInstance(app.screen, FormDialog)
        finally:
            front.close()
            back.close()

    async def test_new_space_accepts_blank_command(self):
        front, back = Pipe()
        app = HerdApp(front)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.press("ctrl+space", "C")
                self.assertIsInstance(app.screen, FormDialog)
                await pilot.press("enter")
                await pilot.pause()
                messages = []
                while back.poll():
                    messages.append(back.recv())
                self.assertTrue(any(message[0] == "create_space" and message[3] == ""
                                    for message in messages), messages)
        finally:
            front.close()
            back.close()


if __name__ == "__main__":
    unittest.main()

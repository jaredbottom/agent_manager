"""How the herd window finds (and if needed starts) the background server."""

import getpass
import os
import secrets
import subprocess
import sys
import tempfile
import time
from multiprocessing.connection import Client
from pathlib import Path

HERD_HOME = Path.home() / ".herd"
LOG_FILE = HERD_HOME / "server.log"


def address():
    if os.name == "nt":
        return rf"\\.\pipe\herd-{getpass.getuser()}"
    return os.path.join(tempfile.gettempdir(), f"herd-{os.getuid()}.sock")


def authkey():
    """Shared secret so only this user's herd processes can talk to the server."""
    path = HERD_HOME / "authkey"
    if not path.exists():
        HERD_HOME.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
    return path.read_bytes()


def try_connect():
    try:
        return Client(address(), authkey=authkey())
    except Exception:
        return None


def start_server():
    HERD_HOME.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "ab")
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=Path.home())
    argv = [sys.executable, "-m", "agent_manager.server"]
    if os.name == "nt":
        # own hidden console + process group, so closing the terminal window doesn't take it down
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:  # the parent's job object doesn't allow breakaway
            subprocess.Popen(argv, creationflags=flags, **kwargs)
    else:
        subprocess.Popen(argv, start_new_session=True, **kwargs)


def connect_or_start(timeout=10):
    conn = try_connect()
    if conn:
        return conn
    start_server()
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.1)
        conn = try_connect()
        if conn:
            return conn
    raise RuntimeError(f"herd server didn't start - see {LOG_FILE}")

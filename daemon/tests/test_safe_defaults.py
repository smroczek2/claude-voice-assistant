"""Spoken text must never auto-approve tools: the default permission mode asks."""
import re
from pathlib import Path

import tomllib

from voz.config import DEFAULTS

DAEMON = Path(__file__).resolve().parent.parent


def test_default_permission_mode_asks_before_every_tool():
    assert DEFAULTS["claude"]["permission_mode"] == "default"
    assert tomllib.loads((DAEMON / "config.toml").read_text())["claude"]["permission_mode"] == "default"


def test_command_line_cannot_bypass_permissions():
    source = (DAEMON / "voz" / "app.py").read_text()
    choices = re.search(r'"--permission-mode", choices=(\[[^\]]*\])', source).group(1)
    assert "bypassPermissions" not in choices and "acceptEdits" not in choices


def test_daemon_state_file_is_written_owner_only():
    source = (DAEMON / "voz" / "daemon.py").read_text()
    assert "open_private(private_dir(Path.home() / \".voz\") / \"daemon.json\")" in source

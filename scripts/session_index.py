"""Read the Claude Desktop app's own record of each Code session.

The app writes one metadata file per session under
~/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/.
It holds the title, the transcript id (cliSessionId), the parent session,
the PRs, and the app's own post-turn summary (status and needed action).
The transcript itself lives in ~/.claude/projects/<cwd-slug>/<cliSessionId>.jsonl
and, unlike list_events, keeps the full tool arguments (wrap_up asks,
AskUserQuestion choices).
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
from dataclasses import dataclass, field

APP_DIR = os.path.expanduser("~/Library/Application Support/Claude")
# Overrides for Macs where Desktop or Claude Code keep their records elsewhere.
SESSIONS_DIR = os.path.expanduser(os.environ.get("VOZ_DESKTOP_SESSIONS_DIR") or os.path.join(APP_DIR, "claude-code-sessions"))
APP_LOG_DIR = os.path.expanduser("~/Library/Logs/Claude")
PROJECTS_DIR = os.path.join(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"), "projects")


def missing_sources() -> list[str]:
    """Folders the ledger needs that do not exist on this Mac, for a plain warning."""
    return [path for path in (SESSIONS_DIR, PROJECTS_DIR) if not os.path.isdir(path)]


@dataclass
class SessionIndex:
    by_local: dict = field(default_factory=dict)
    by_cli: dict = field(default_factory=dict)


def record_paths(name: str = "local_*.json") -> list[str]:
    """Session record files of the Claude account that is in use now.

    The app keeps one folder per account and writes only to the account that is signed in. A copy
    of a record in another account folder is a frozen snapshot: it can hold a card or a question
    that the app no longer shows. On 2026-11-11, after an account switch, two such cards were
    read aloud as waiting. The account in use is the one with the newest record file."""
    newest, active = 0.0, None
    for account in glob.glob(os.path.join(SESSIONS_DIR, "*")):
        stamp = 0.0
        for path in glob.glob(os.path.join(account, "*", "local_*.json")):
            try:
                stamp = max(stamp, os.stat(path).st_mtime)
            except OSError:
                continue
        if stamp > newest:
            newest, active = stamp, account
    return glob.glob(os.path.join(active, "*", name)) if active else []


def load_session_index() -> SessionIndex:
    """Index every session file of the account in use; when an id appears in several folders, keep the newest."""
    index = SessionIndex()
    for path in record_paths():
        try:
            with open(path, encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        local_id = meta.get("sessionId")
        if not local_id:
            continue
        known = index.by_local.get(local_id)
        if known and known.get("lastActivityAt", 0) >= meta.get("lastActivityAt", 0):
            continue
        index.by_local[local_id] = meta
    for meta in index.by_local.values():
        for cli_id in [meta.get("cliSessionId"), *(meta.get("priorCliSessionIds") or [])]:
            if cli_id:
                index.by_cli[cli_id] = meta
    return index


def transcript_path(meta: dict) -> str | None:
    """Find the transcript file for a session, whatever folder slug the CLI used."""
    cli_id = meta.get("cliSessionId")
    if not cli_id:
        return None
    matches = glob.glob(os.path.join(PROJECTS_DIR, "*", f"{cli_id}.jsonl"))
    return max(matches, key=os.path.getmtime) if matches else None


def parse_iso(value: str) -> dt.datetime | None:
    """Parse transcript timestamps such as 2026-11-06T14:58:04.686Z on Python 3.10."""
    if not value:
        return None
    if value.endswith("Z"):
        value = value.removesuffix("Z") + "+00:00"
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None

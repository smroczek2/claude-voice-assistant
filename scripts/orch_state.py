"""Build the orchestrator state by code, with no model call.

The builder reads the Desktop app's session records and the transcript files.
It writes `state.json`, `digest.md` and `cache-v<N>.json` in the state folder
(`~/.claude/orchestrator/`, or `VOZ_ORCH_DIR`). It is the one writer of these
files: it holds a file lock for the whole build and replaces each file with an
atomic rename. No model and no person edits them.

`orchestrators.json` in the same folder names the orchestrator sessions:
either a JSON list of session ids, or {"sessions": [...], "vault_dir": "..."}.
Each id is a Desktop id (local_...) or a transcript id.

Facts in the state (each has a source and a time):
  alert      an orchestrator that is archived, has an open bound PR, or is not found
  question   a question open in another session
  card       a card that waits for a click, in any session not archived (from its record)
  idle_pr    a session idle more than one day and less than two weeks, with an open PR
  vault      the date of the newest note in the vault, and if it is stale
  guards     (as alert) a child that runs another model or folder than its orchestrator asked for,
             or a murmur such as "mm-hmm" relayed as a decision; (as idle_pr) a child of an
             orchestrator idle more than one day with its work not finished

Not a fact: "decisions", the newest decisions that orch_mine.py mined from the
orchestrator transcripts. The start digest shows them below the facts.

Commands:
  build                    build now (waits for the lock)
  show [--budget start|turn]  print the digest from the saved state
  install-rules [--force]  copy orchestrator/rules.md from this repo to the state folder

The folder layout is in orchestrator/CONTEXT.md.

Runs on the macOS system Python 3.9 with the standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger  # noqa: E402
import orch_guards  # noqa: E402
import session_index  # noqa: E402

# The state keeps version 1 so the code of version 1 reads it as "last good" after a rollback.
# That code ignores "roster"; this code needs it, so a state without it is built again.
VERSION = 1
# The cache file name carries its schema, so code with another schema never reads it.
# A file with another schema inside, or a cached item with another shape, is dropped and read again.
CACHE_SCHEMA = 2
CACHE_FILE = f"cache-v{CACHE_SCHEMA}.json"
SCAN_KEYS = {"path", "inode", "offset", "turns", "open"}
BUDGETS = {"start": 3000, "turn": 600}
STALE_S = 15 * 60
QUESTION_WINDOW_S = 3 * 86400
IDLE_PR_S = 86400
IDLE_PR_MAX_S = 14 * 86400  # older than this is abandoned work, not stalled work; it is not news
CARD_EXPIRE_S = 7 * 86400
KEEP_TURNS = 200
LAST_TURNS = 15
DECISIONS_BUDGET = 400  # tokens at most for the decisions in the start digest
DECISIONS_HEADING = "DECISIONS (newest first: the user's words, and the question before them)"
HEAD_RESERVE = 30  # tokens kept for the STATE line, so the cut does not move when the line changes
PRIORITY = {"alert": 0, "question": 1, "card": 2, "idle_pr": 3, "vault": 4}
HEADINGS = {"alert": "ALERTS", "question": "QUESTIONS OPEN IN OTHER SESSIONS", "card": "CARDS WAITING FOR A CLICK",
            "idle_pr": "IDLE MORE THAN 1 DAY WITH WORK OPEN",
            "vault": "VAULT"}
INJECTED_PREFIXES = ('{"source":"voz-runtime"', '{"source": "voz-runtime"', "STATE ")
# A turn that matches a rule asks for an action. It is done only when a tool call whose name
# or input holds one of the rule's words follows it before the next turn. "" matches any tool call.
# Merge and approve also accept a relay: the orchestrator sends them to the child that does the work.
ACTION_RULES = [
    (r"\bmerge", ("merge", "sendmessage", "send_message")),
    (r"\b(send|tell|relay|message|reply to|answer)\b", ("sendmessage", "send_message", "slack_send", "create_draft")),
    (r"\b(start|kick off|spin up|launch)\b", ("start_session", "workflow", "agent")),
    (r"\barchive", ("archive",)),
    (r"\b(post|comment)\b", ("slack_send", "comment", "post")),
    (r"\bapprove", ("approve", "sendmessage", "send_message")),
    (r"\b(push|commit)\b", ("git push", "git commit", "commit")),
    (r"\b(ticket|issue)\b", ("save_issue", "issue")),
    (r"\b(close|cancel|stop|kill|dismiss)\b", ("stop", "close", "archive", "dismiss", "cancel")),
    (r"\b(fix|build|update|change|add|remove|delete|run|check|write|create|open|schedule|remind|pin)\b", ("",)),
]


class StateError(Exception):
    """A fault that stops the build. The message says what to fix."""


# ---------- paths ----------

def state_dir() -> str:
    return os.path.expanduser(os.environ.get("VOZ_ORCH_DIR") or "~/.claude/orchestrator")


def path_of(name: str) -> str:
    return os.path.join(state_dir(), name)


def now_s() -> float:
    return time.time()


def epoch(iso_text: str) -> float:
    parsed = session_index.parse_iso(iso_text or "")
    return parsed.timestamp() if parsed else 0.0


def clock(seconds: float, with_day: bool = True) -> str:
    if not seconds:
        return "unknown time"
    local = dt.datetime.fromtimestamp(seconds).astimezone()
    return local.strftime("%m-%d %H:%M" if with_day else "%H:%M")


def tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


# ---------- files ----------

def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def write_atomic(path: str, text: str) -> None:
    """Write a whole file with a rename, so a reader sees the old file or the new one, never half."""
    folder = os.path.dirname(path)
    fd, temp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=os.path.basename(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def load_orchestrators() -> tuple[list[str], str]:
    """The orchestrator ids and the vault folder. A missing file means no orchestrator."""
    path = path_of("orchestrators.json")
    try:
        data = read_json(path, [])
    except json.JSONDecodeError as error:
        raise StateError(f"{path} is not valid JSON ({error})") from error
    vault = os.environ.get("VOZ_VAULT_DIR", "")
    if isinstance(data, dict):
        vault = str(data.get("vault_dir") or vault)
        data = data.get("sessions", [])
    if not isinstance(data, list) or not all(isinstance(item, str) and item for item in data):
        raise StateError(f"{path} must be a list of session ids, or an object with a 'sessions' list")
    return list(dict.fromkeys(data)), os.path.expanduser(vault)


# ---------- session records ----------

def slim(meta: dict) -> dict:
    """The few fields the state needs. Records can be large, so the cache keeps only these."""
    summary = meta.get("postTurnSummary") or {}
    return {
        "sessionId": meta.get("sessionId", ""), "cliSessionId": meta.get("cliSessionId", ""),
        "priorCliSessionIds": meta.get("priorCliSessionIds") or [], "title": meta.get("title", ""),
        "isArchived": bool(meta.get("isArchived")), "lastActivityAt": meta.get("lastActivityAt", 0),
        "scheduledTaskId": meta.get("scheduledTaskId") or "",
        "prs": [{"number": p.get("prNumber"), "repo": p.get("repo", ""), "state": str(p.get("state", "")).upper()}
                for p in meta.get("prs") or [] if isinstance(p, dict)],
        "cwd": meta.get("cwd", ""),
        "cards": waiting_cards(meta),
        "resolvedCards": sorted(str(k) for k in (meta.get("resolvedBackgroundTaskSuggestions") or {})),
        "startedFromCard": str((meta.get("spawnedFrom") or {}).get("taskId") or ""),
        "postTurnSummary": {k: summary.get(k) for k in ("status_category", "needs_action", "status_detail")},
        "priorErrorMark": {"at": (meta.get("priorErrorMark") or {}).get("at", 0)},
        "lastTurnEdgeAt": meta.get("lastTurnEdgeAt", 0), "lastTurnStopped": bool(meta.get("lastTurnStopped")),
        **orch_guards.record_fields(meta),
    }


def load_cache() -> dict:
    """The cache, or an empty one when it is missing, broken, or has another schema."""
    try:
        cache = read_json(path_of(CACHE_FILE), {})
    except json.JSONDecodeError:
        return {}
    if not isinstance(cache, dict) or cache.get("schema") != CACHE_SCHEMA:
        return {}
    return {key: value for key, value in cache.items() if isinstance(value, dict) or key == "schema"}


def waiting_cards(meta: dict) -> list[dict]:
    """The cards the app still shows for this session: proposed, not started, not dismissed."""
    raw = meta.get("backgroundTaskSuggestions") or []
    items = raw.values() if isinstance(raw, dict) else raw
    resolved = meta.get("resolvedBackgroundTaskSuggestions") or {}
    out = []
    for card in items:
        if isinstance(card, dict) and card.get("id") and card["id"] not in resolved:
            out.append({"id": str(card["id"]), "title": str(card.get("title", "")),
                        "at": (card.get("createdAt") or 0) / 1000, "expires": (card.get("expiresAt") or 0) / 1000})
    return out


SLIM_KEYS = set(slim({}))


def read_records(cache: dict) -> tuple[dict, list[str]]:
    """Every session record of the account in use, newest copy per id. Unchanged files come from the cache."""
    folder = session_index.SESSIONS_DIR
    if not os.path.isdir(folder):
        raise StateError(f"session records folder not found: {folder}")
    old, fresh, records, unreadable = cache.get("records", {}), {}, {}, []
    for path in session_index.record_paths():
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        hit = old.get(path)
        if isinstance(hit, dict) and hit.get("mtime") == mtime and isinstance(hit.get("meta"), dict) \
                and set(hit["meta"]) == SLIM_KEYS:
            fresh[path] = hit
        else:
            try:
                with open(path, encoding="utf-8") as handle:
                    fresh[path] = {"mtime": mtime, "meta": slim(json.load(handle))}
            except (OSError, json.JSONDecodeError):
                unreadable.append(path)
                continue
        meta = fresh[path]["meta"]
        known = records.get(meta["sessionId"])
        if meta["sessionId"] and (not known or known["lastActivityAt"] < meta["lastActivityAt"]):
            records[meta["sessionId"]] = meta
    cache["records"] = fresh
    return drop_clicked(records, [hit["meta"] for hit in fresh.values()]), unreadable


def clicked_cards(copies: list[dict]) -> tuple[dict, set]:
    """The card ids resolved in any copy of each record, and the card ids a session started from."""
    resolved, started = {}, set()
    for meta in copies:
        resolved.setdefault(meta["sessionId"], set()).update(meta.get("resolvedCards", []))
        if meta.get("startedFromCard"):
            started.add(meta["startedFromCard"])
    return resolved, started


def drop_clicked(records: dict, copies: list[dict]) -> dict:
    """Keep only the cards that no copy marks as clicked, dismissed or expired, and that no session started from.

    read_records keeps the newest copy of each record. Another copy in the same account can hold the click,
    and a child session names its card in spawnedFrom before the holder records the click."""
    resolved, started = clicked_cards(copies)
    out = {}
    for local_id, meta in records.items():
        done = resolved.get(local_id, set()) | started
        cards = [card for card in meta.get("cards", []) if card["id"] not in done]
        out[local_id] = {**meta, "cards": cards} if len(cards) != len(meta.get("cards", [])) else meta
    return out


def still_waiting(cards: list[dict], since: float, now: float) -> list[dict]:
    """Check the cards again at this moment, for the Stop hook. The saved state can be 2 minutes old.

    Reads each holder's record in the account in use, and each record changed since `since` (a new
    child session from a click). Drops a card that is clicked, dismissed, expired, gone, or started."""
    copies, seen = [], set()
    for holder in {card["holder"] for card in cards}:
        for path in session_index.record_paths(f"{holder}.json"):
            seen.add(path)
            copies.extend(read_record_copy(path))
    for path in session_index.record_paths():
        try:
            changed = path not in seen and os.stat(path).st_mtime >= since - 5
        except OSError:
            continue
        if changed:
            copies.extend(read_record_copy(path))
    resolved, started = clicked_cards(copies)
    shown = {(meta["sessionId"], card["id"]) for meta in copies for card in meta.get("cards", [])}
    return [card for card in cards
            if (card["holder"], card["id"]) in shown and card["id"] not in resolved.get(card["holder"], set())
            and card["id"] not in started and now < card["expires"]]


def read_record_copy(path: str) -> list[dict]:
    """One record copy, slimmed, or nothing when the file is gone or half written."""
    try:
        with open(path, encoding="utf-8") as handle:
            return [slim(json.load(handle))]
    except (OSError, json.JSONDecodeError):
        return []


def resolve(ids: list[str], records: dict) -> tuple[dict, list[str]]:
    """Map each configured id to its local id. Returns {local_id: meta} and the ids not found."""
    by_cli = {}
    for meta in records.values():
        for cli in [meta["cliSessionId"], *meta["priorCliSessionIds"]]:
            if cli:
                by_cli[cli] = meta
    found, missing = {}, []
    for item in ids:
        meta = records.get(item) or by_cli.get(item)
        if meta:
            found[meta["sessionId"]] = meta
        else:
            missing.append(item)
    return found, missing


def find_transcript(meta: dict, cache: dict) -> str | None:
    paths = cache.setdefault("transcripts", {})
    known = paths.get(meta["cliSessionId"])
    if known and os.path.exists(known):
        return known
    found = session_index.transcript_path(meta) if meta["cliSessionId"] else None
    if found:
        paths[meta["cliSessionId"]] = found
    return found


# ---------- orchestrator transcript (read from the last offset) ----------

def new_scan(path: str, inode: int) -> dict:
    return {"path": path, "inode": inode, "offset": 0, "turns": [], "open": False}


def is_injected(text: str) -> bool:
    return text.lstrip().startswith(INJECTED_PREFIXES)


def tool_signature(block: dict) -> str:
    name = str(block.get("name", ""))
    body = json.dumps(block.get("input") or {}, ensure_ascii=False)[:200]
    return f"{name} {body}"


def assistant_block(block: dict, scan: dict) -> None:
    turn = scan["turns"][-1] if scan["open"] and scan["turns"] else None
    if block.get("type") == "text" and block.get("text", "").strip():
        if turn is not None:
            turn["replied"] = True
        return
    if block.get("type") != "tool_use":
        return
    if turn is not None and len(turn["tools"]) < 12:
        turn["tools"].append(tool_signature(block))


def consume(entry: dict, scan: dict) -> None:
    content = (entry.get("message") or {}).get("content")
    if entry.get("type") == "assistant" and isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                assistant_block(block, scan)
        return
    if ledger.is_tool_result(content):
        return
    message = ledger.person_message(entry)
    if not message or is_injected(message["text"]):
        return
    if message["from"] != ledger.USER_FROM:
        scan["open"] = False  # a relay from another session; later tools answer it, not the user
        return
    scan["turns"].append({"text": message["text"][:600], "at": epoch(message["at"]), "tools": [], "replied": False})
    scan["turns"] = scan["turns"][-KEEP_TURNS:]
    scan["open"] = True


def scan_orchestrator(path: str, prior: dict | None) -> dict:
    """Read only the lines added since the last build. A replaced or shorter file is read again."""
    info = os.stat(path)
    scan = prior if isinstance(prior, dict) and SCAN_KEYS <= set(prior) and prior.get("path") == path and prior.get("inode") == info.st_ino \
        and prior.get("offset", 0) <= info.st_size else new_scan(path, info.st_ino)
    with open(path, "rb") as handle:
        handle.seek(scan["offset"])
        data = handle.read()
    end = data.rfind(b"\n") + 1
    for raw in data[:end].split(b"\n"):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            consume(entry, scan)
    scan["offset"] += end
    return scan


# ---------- turns and actions ----------

def asked_patterns(text: str) -> list[str] | None:
    """The tool words that would do what the turn asks, or None when the turn asks for no action."""
    lowered, patterns = text.lower(), []
    for rule, words in ACTION_RULES:
        if re.search(rule, lowered):
            patterns.extend(words)
    return patterns or None


def action_done(turn: dict) -> bool | None:
    """True or False for a turn that asks for an action; None for a turn that asks for none."""
    patterns = asked_patterns(turn["text"])
    if patterns is None:
        return None
    tools = [signature.lower() for signature in turn["tools"]]
    return any(word in signature for word in patterns for signature in tools)


def short_tool(signature: str) -> str:
    name = signature.split(" ", 1)[0]
    return name.split("__")[-1] if name.startswith("mcp__") else name


def turn_label(turn: dict) -> str:
    done = action_done(turn)
    names = list(dict.fromkeys(short_tool(s) for s in turn["tools"]))[:3]
    if done is False:
        return "no action followed" + (f" (only {', '.join(names)})" if names else "")
    if names:
        return "action: " + ", ".join(names)
    return "answered" if turn["replied"] else "no action followed"


# ---------- facts ----------

def fact(kind: str, text: str, source: str, at: float, priority: int | None = None) -> dict:
    return {"kind": kind, "priority": PRIORITY[kind] if priority is None else priority,
            "text": text, "source": source, "at": at}


def orchestrator_facts(found: dict, missing: list[str], now: float) -> list[dict]:
    facts = [fact("alert", f"Orchestrator id {item} is not in the session records. Check orchestrators.json.",
                  "orchestrators.json", now) for item in missing]
    for local_id, meta in found.items():
        record = f"session record {local_id}"
        if meta["isArchived"]:
            facts.append(fact("alert", f"Orchestrator '{meta['title']}' is archived.", record, meta["lastActivityAt"] / 1000))
        # No alert for a MERGED or CLOSED bound PR. Assumption: the app auto-archives a
        # session when a bound PR closes, not later. One orchestrator had 16 closed PRs
        # bound and stayed unarchived (2026-11-10). If the app is ever seen to archive a
        # session for a PR that was already closed, the closed-PR alert must return.
        # A missing or unknown state can be open, so it gets the alert.
        for pr in meta["prs"]:
            if pr["state"] in ("MERGED", "CLOSED"):
                continue
            label = "open PR" if pr["state"] == "OPEN" else "PR"
            unknown = "" if pr["state"] == "OPEN" else " (state unknown)"
            facts.append(fact("alert", f"Orchestrator '{meta['title']}' has {label} {pr['repo']}#{pr['number']} bound"
                                       f"{unknown}. Closing it can auto-archive the orchestrator.",
                              record, meta["lastActivityAt"] / 1000))
    return facts


def age_text(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h" if minutes < 48 * 60 else f"{minutes // 1440} d"


def card_facts(records: dict, now: float) -> list[dict]:
    """Every card that waits for a click, in any session that is not archived, with its holder's link."""
    out = []
    for local_id, meta in records.items():
        if meta["isArchived"]:
            continue
        for card in meta.get("cards", []):
            expires = card["expires"] or card["at"] + CARD_EXPIRE_S
            if now >= expires:
                continue
            item = fact("card", f"'{card['title']}' waiting {age_text(now - card['at'])}, held by "
                                f"[{meta['title'] or local_id}](#{local_id}) ({card['id']}).",
                        f"session record {local_id}", card["at"])
            item["card"] = {"id": card["id"], "title": card["title"], "holder": local_id,
                            "holder_title": meta["title"] or local_id, "at": card["at"], "expires": expires}
            out.append(item)
    return out


def question_facts(records: dict, orchestrators: dict, cache: dict, now: float) -> list[dict]:
    """Open questions in other recent sessions. Each transcript is read again only when it changed."""
    old, fresh, out = cache.get("status", {}), {}, []
    for local_id, meta in records.items():
        if local_id in orchestrators or meta["isArchived"] or meta["scheduledTaskId"]:
            continue
        if now - meta["lastActivityAt"] / 1000 > QUESTION_WINDOW_S:
            continue
        path = find_transcript(meta, cache)
        key = f"{path}:{os.path.getmtime(path) if path else 0}:{meta['lastActivityAt']}"
        hit = old.get(local_id)
        if not hit or hit["key"] != key:
            facts = ledger.scan_transcript(path) if path else {"question": None, "closing": ""}
            question = facts.get("question")
            hit = {"key": key, "status": ledger.status_of(meta, facts, question),
                   "text": (question or {}).get("text") or meta["postTurnSummary"].get("needs_action") or "",
                   "at": epoch((question or {}).get("at", "")) or meta["lastActivityAt"] / 1000}
        fresh[local_id] = hit
        if hit["status"] == "needs you":
            out.append(fact("question", f"'{meta['title']}': {hit['text'][:200]} (since {clock(hit['at'])}).",
                            f"session {local_id}", hit["at"]))
    cache["status"] = fresh
    return out


def idle_pr_facts(records: dict, orchestrators: dict, now: float) -> list[dict]:
    out = []
    for local_id, meta in records.items():
        if local_id in orchestrators or meta["isArchived"]:
            continue
        idle = now - meta["lastActivityAt"] / 1000
        open_prs = [p for p in meta["prs"] if p["state"] == "OPEN"]
        if IDLE_PR_S < idle < IDLE_PR_MAX_S and open_prs:
            prs = ", ".join(f"{p['repo']}#{p['number']}" for p in open_prs)
            out.append(fact("idle_pr", f"'{meta['title']}' idle since {clock(meta['lastActivityAt'] / 1000)}, open PR {prs}.",
                            f"session record {local_id}", meta["lastActivityAt"] / 1000))
    return out


def previous_working_day(day: dt.date) -> dt.date:
    day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def vault_facts(vault: str, now: float) -> list[dict]:
    if not vault:
        return []
    if not os.path.isdir(vault):
        return [fact("alert", f"Vault folder not found: {vault}.", "orchestrators.json", now)]
    newest = 0.0
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.endswith(".md"):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, name)))
                except OSError:
                    continue
    if not newest:
        return [fact("vault", "The vault has no notes.", vault, now, priority=PRIORITY["question"])]
    newest_day = dt.datetime.fromtimestamp(newest).date()
    if newest_day < previous_working_day(dt.datetime.fromtimestamp(now).date()):
        return [fact("vault", f"Vault stale since {newest_day:%m-%d}. Use live connectors.", vault, newest,
                     priority=PRIORITY["question"])]
    return [fact("vault", f"Vault newest note {newest_day:%m-%d}.", vault, newest)]


def guard_facts(records: dict, found: dict, scans: dict, cache: dict, now: float) -> list[dict]:
    """Model, folder, stalled-child and murmur guards. Their rows use kinds the state already has.

    A fault in a guard never fails the build: it drops the guards' cache part, so the next build
    reads again from the start, and shows as one alert beside the other facts.
    """
    part = cache.pop("guards", None)
    part = part if isinstance(part, dict) else {}
    try:
        rows = orch_guards.rows(records, found, scans, part, now, lambda meta: find_transcript(meta, cache), clock)
    except Exception as error:  # noqa: BLE001 - reported as an alert, not swallowed
        return [fact("alert", f"Guards failed: {type(error).__name__}: {error}. The other facts are fine.",
                     "scripts/orch_guards.py", now)]
    cache["guards"] = part
    return [fact(kind, text, source, at) for kind, text, source, at in rows]


# ---------- build ----------

def collect(ids: list[str], vault: str, cache: dict, now: float) -> dict:
    records, unreadable = read_records(cache)
    found, missing = resolve(ids, records)
    old_scans, scans, last_turns, cli_to_local = cache.get("scans", {}), {}, {}, {}
    for local_id, meta in found.items():
        for cli in [meta["cliSessionId"], *meta["priorCliSessionIds"]]:
            if cli:
                cli_to_local[cli] = local_id
        path = find_transcript(meta, cache)
        if path:
            scans[local_id] = scan_orchestrator(path, old_scans.get(local_id))
            last_turns[local_id] = [{"text": t["text"][:240], "at": t["at"], "label": turn_label(t)}
                                    for t in scans[local_id]["turns"][-LAST_TURNS:]]
    cache["scans"] = scans
    facts = orchestrator_facts(found, missing, now)
    facts += card_facts(records, now)
    facts += question_facts(records, found, cache, now)
    facts += idle_pr_facts(records, found, now)
    facts += vault_facts(vault, now)
    facts += guard_facts(records, found, scans, cache, now)
    if unreadable:
        facts.append(fact("alert", f"{len(unreadable)} session record(s) could not be read, for example "
                                   f"{os.path.basename(unreadable[0])}.", session_index.SESSIONS_DIR, now))
    facts.sort(key=lambda f: (f["priority"], -f["at"]))
    roster = [{"id": local_id, "title": meta["title"], "cwd": meta["cwd"]} for local_id, meta in found.items()]
    return {"version": VERSION, "built_at": now, "orchestrators": sorted(found), "roster": roster,
            "cli_to_local": cli_to_local,
            "facts": facts, "last_turns": last_turns}


def build(started: float | None = None) -> dict:
    """Build under the lock. A build that finished after this call began is reused, not repeated."""
    started = started or now_s()
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    with open(path_of(".lock"), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            current = load_state()
            if current and current.get("built_at", 0) >= started:
                return current
            ids, vault = load_orchestrators()
            cache = load_cache()  # the cache is only a speed-up; a bad one is rebuilt from the sources
            cache["schema"] = CACHE_SCHEMA
            state = collect(ids, vault, cache, now_s())
            add_decisions(state, cache)
            state["build_ms"] = int((now_s() - started) * 1000)
            write_atomic(path_of(CACHE_FILE), json.dumps(cache, separators=(",", ":")))
            write_atomic(path_of("state.json"), json.dumps(state, ensure_ascii=False, indent=1))
            write_atomic(path_of("digest.md"), render(state, "start", now_s()) + "\n")
            return state
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def add_decisions(state: dict, cache: dict) -> None:
    """Run the miners on the orchestrator transcripts and keep the newest decisions in the state.

    The key "decisions" is new in version 1: older code ignores it, so a rollback still reads this state.
    A miner fault does not stop the build. It becomes an alert, with the command to run it alone.
    """
    import orch_mine  # here, not at the top: orch_mine imports this module

    targets = {local_id: scan["path"] for local_id, scan in cache.get("scans", {}).items()}
    try:
        state["decisions"] = orch_mine.mine(targets)
    except Exception as error:  # noqa: BLE001 -- reported in the digest, never silent
        state["facts"].insert(0, fact("alert", f"The decision miner failed ({type(error).__name__}: {error}). "
                                               "Run: python3 scripts/orch_mine.py mine", "orch_mine.py", now_s()))


def load_state() -> dict | None:
    try:
        state = read_json(path_of("state.json"), None)
    except json.JSONDecodeError:
        return None
    if not isinstance(state, dict) or state.get("version") != VERSION or "roster" not in state:
        return None
    return state


# ---------- digest ----------

def stamp(state: dict, now: float, note: str = "") -> str:
    built = state.get("built_at", 0)
    age_min = int((now - built) // 60)
    line = f"STATE as of {clock(built, with_day=False)}"
    if now - built > STALE_S:
        line += f" (STALE: {age_min} min old, facts not verified)"
    return line + (f" ({note})" if note else "")


def body(state: dict, budget: int, reserve: int = 0) -> str:
    """The facts, highest priority first, cut to the budget, with a "+N more" line for the rest."""
    facts = [item for item in state.get("facts", []) if item["kind"] in HEADINGS]  # a state from older code can hold more kinds
    lines, used, shown, heading = [], reserve, 0, None
    more_line = 12  # tokens kept free for the "+N more" line
    for item in facts:
        new_heading = HEADINGS[item["kind"]]
        row = f"- {item['text']} [{item['source']}]"
        cost = tokens(row) + 1 + (tokens(new_heading) + 1 if new_heading != heading else 0)
        if used + cost > budget - more_line:
            break
        if new_heading != heading:
            lines.append(new_heading)
            heading = new_heading
        lines.append(row)
        used += cost
        shown += 1
    if shown < len(facts):
        lines.append(f"+{len(facts) - shown} more (python3 scripts/orch_state.py show --budget start)")
    return "\n".join(lines) if lines else "No open loops."


def decisions_block(state: dict, budget: int) -> str:
    """The newest decisions, newest first, cut to the budget. Empty when there are none."""
    lines, used = [DECISIONS_HEADING], tokens(DECISIONS_HEADING) + 1
    for item in reversed(state.get("decisions") or []):
        asked = f" to \"{item['question'][:120]}\"" if item.get("question") else ""
        row = (f"- {clock(item.get('at', 0))} {item['kind']}: \"{item['words'][:160]}\"{asked} "
               f"[{item['channel']}, {str(item['session'])[:14]}]")
        if used + tokens(row) + 1 > budget:
            break
        lines.append(row)
        used += tokens(row) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


def render(state: dict, mode: str, now: float, note: str = "", budget: int | None = None) -> str:
    """The STATE line and the facts. At start, the newest decisions follow, below the priority sections."""
    total = BUDGETS[mode] if budget is None else budget
    decisions = decisions_block(state, min(DECISIONS_BUDGET, total // 4)) if mode == "start" else ""
    room = total - (tokens(decisions) + 1 if decisions else 0)
    text = stamp(state, now, note) + "\n" + body(state, room, HEAD_RESERVE)
    return text + ("\n" + decisions if decisions else "")


def digest_hash(state: dict, mode: str = "turn") -> str:
    return hashlib.sha256(body(state, BUDGETS[mode], HEAD_RESERVE).encode("utf-8")).hexdigest()


RULES_SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orchestrator", "rules.md")


def install_rules(force: bool) -> int:
    """Copy the rules source to the state folder. A changed copy there is kept unless --force."""
    with open(RULES_SOURCE, encoding="utf-8") as handle:
        text = handle.read()
    target = path_of("rules.md")
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    if os.path.exists(target):
        with open(target, encoding="utf-8") as handle:
            current = handle.read()
        if current == text:
            print(f"{target} is current.")
            return 0
        if not force:
            print(f"{target} differs from {RULES_SOURCE}. Move your edits into the source, "
                  "or run again with --force (the old copy goes to rules.md.bak).", file=sys.stderr)
            return 1
        write_atomic(target + ".bak", current)
    write_atomic(target, text)
    print(f"Installed {target}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orchestrator state builder")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    show = sub.add_parser("show")
    show.add_argument("--budget", choices=sorted(BUDGETS), default="start")
    install = sub.add_parser("install-rules")
    install.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "install-rules":
        return install_rules(args.force)
    try:
        if args.command == "build":
            state = build()
            print(f"Built in {state.get('build_ms', 0)} ms: {len(state['facts'])} facts.")
            return 0
        state = load_state()
        if not state:
            print("STATE BUILD FAILED: no saved state. Run: python3 scripts/orch_state.py build")
            return 1
        print(render(state, args.budget, now_s()))
        return 0
    except StateError as error:
        print(f"STATE BUILD FAILED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

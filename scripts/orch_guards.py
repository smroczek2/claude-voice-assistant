"""Guards on the children of an orchestrator, checked by code with no model call.

`orch_state.py` calls this module during each build. It reads what an
orchestrator asked for in its `start_session` calls, and compares it with what
the child session got:

  model    the model the child runs (its transcript, else its record) against the
           `model` the orchestrator passed. A build child (one with a worktree or a
           branch) with no `model` must run BUILD_FAMILY.
  folder   the folder the child works in against the `cwd` the orchestrator passed,
           else the folder its brief says to change to, else the orchestrator's folder
           project (a card always starts there; a worktree counts as its repo).
  stalled  a child idle more than one day whose work is not finished.
  murmur   a turn that is only a murmur ("mm-hmm") that the orchestrator relayed,
           merged or approved as if it were a decision.

Each guard returns (kind, text, source, at) rows. `orch_state.py` turns them into
facts with the kinds it already has, so the state stays version 1.

Runs on the macOS system Python 3.9 with the standard library only.
"""

from __future__ import annotations

import json
import os
import re

BUILD_FAMILY = "opus"
FAMILIES = ("opus", "sonnet", "haiku", "fable")
WINDOW_S = 2 * 86400  # children started longer ago than this get no model or folder check
GRACE_S = 10 * 60  # a new child gets this long to change folder before the folder check
STALL_S = 86400
STALL_WINDOW_S = 7 * 86400  # a child idle longer than this is old history, not a stalled child
MURMUR_WINDOW_S = 2 * 86400
KEEP_STARTS = 200
TAIL_BYTES = 256 * 1024
SCAN_KEYS = {"path", "inode", "offset", "pending", "starts"}
START_TOOL = "start_session"
SESSION_RE = re.compile(r"session_id: (local_[0-9A-Za-z-]+)")
TASK_RE = re.compile(r"task_id: (task_[0-9A-Za-z]+)")
# A brief that cannot set the folder tells the child to change to it: "call `..change_directory` to `/path`".
CHANGE_DIR_RE = re.compile(r"change_directory`?[^`\n]{0,40}`((?:~|/)[^`\n]+)`")
MURMUR_RE = re.compile(r"^(m+-?hm+|mhm+|hm+|uh-?huh|um+|uh+|mm+)[.!?,\s]*$", re.IGNORECASE)
DECISION_TOOLS = ("sendmessage", "send_message", "merge", "approve")


def record_fields(meta: dict) -> dict:
    """The record fields the guards need. `orch_state.slim` adds them to each cached record."""
    spawned = meta.get("spawnedFrom") or {}
    return {"model": str(meta.get("model") or ""), "originCwd": meta.get("originCwd") or "",
            "worktreePath": meta.get("worktreePath") or "", "createdAt": meta.get("createdAt") or 0,
            "spawnedFrom": {"sessionId": spawned.get("sessionId") or "", "taskId": spawned.get("taskId") or ""}}


# ---------- what the orchestrator asked for ----------

def new_scan(path: str, inode: int) -> dict:
    return {"path": path, "inode": inode, "offset": 0, "pending": {}, "starts": []}


def requested(args: dict, at: str) -> dict:
    prompt = str(args.get("prompt") or "")
    match = CHANGE_DIR_RE.search(prompt)
    folder = str(args.get("cwd") or "") or (match.group(1) if match else "")
    return {"title": str(args.get("title") or ""), "model": str(args.get("model") or ""),
            "folder": folder,
            "build": bool(args.get("use_worktree") or args.get("branch_name")),
            "proposal_id": str(args.get("proposal_id") or ""), "at": at, "session_id": "", "task_id": ""}


def result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def settle(scan: dict, tool_id: str, text: str) -> None:
    """A start_session result names the new session or the card. An error names neither."""
    start = scan["pending"].pop(tool_id, None)
    session, task = SESSION_RE.search(text), TASK_RE.search(text)
    if not start or not (session or task):
        return
    start["session_id"] = session.group(1) if session else ""
    start["task_id"] = task.group(1) if task else ""
    if start["proposal_id"]:  # a card started by its id: the request is the card's own
        for earlier in scan["starts"]:
            if earlier["task_id"] == start["proposal_id"]:
                earlier["session_id"] = start["session_id"] or earlier["session_id"]
                return
    scan["starts"] = (scan["starts"] + [start])[-KEEP_STARTS:]


def consume(entry: dict, scan: dict) -> None:
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and str(block.get("name", "")).endswith(START_TOOL):
            scan["pending"][str(block.get("id", ""))] = requested(block.get("input") or {}, entry.get("timestamp", ""))
        elif block.get("type") == "tool_result" and block.get("tool_use_id") in scan["pending"]:
            settle(scan, block["tool_use_id"], result_text(block.get("content")))


def scan_starts(path: str, prior: dict | None) -> dict:
    """Read only the lines added since the last build. A replaced or shorter file is read again."""
    info = os.stat(path)
    usable = isinstance(prior, dict) and SCAN_KEYS <= set(prior) and prior.get("path") == path \
        and prior.get("inode") == info.st_ino and prior.get("offset", 0) <= info.st_size
    scan = prior if usable else new_scan(path, info.st_ino)
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


# ---------- what the child got ----------

def transcript_tail(path: str | None, old: dict, fresh: dict) -> dict:
    """The last model and folder in a child transcript, read from its end and kept per file version."""
    if not path:
        return {"model": "", "cwd": ""}
    try:
        info = os.stat(path)
    except OSError:
        return {"model": "", "cwd": ""}
    key = f"{path}:{info.st_mtime}:{info.st_size}"
    hit = old.get(path)
    if isinstance(hit, dict) and hit.get("key") == key and {"model", "cwd"} <= set(hit):
        fresh[path] = hit
        return hit
    with open(path, "rb") as handle:
        handle.seek(max(0, info.st_size - TAIL_BYTES))
        lines = handle.read().split(b"\n")
    found = {"key": key, "model": "", "cwd": ""}
    for raw in reversed(lines):
        try:
            entry = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        model = str((entry.get("message") or {}).get("model") or "")
        if not found["model"] and entry.get("type") == "assistant" and model and not model.startswith("<"):
            found["model"] = model
        found["cwd"] = found["cwd"] or str(entry.get("cwd") or "")
        if found["model"] and found["cwd"]:
            break
    fresh[path] = found
    return found


def norm_model(name: str) -> str:
    return re.sub(r"\[.*?\]$", "", name.strip().lower())


def model_ok(expected: str, actual: str) -> bool:
    """An exact id must match; a short name such as "opus" or "opus-5-5" matches any id that holds it."""
    want, have = norm_model(expected), norm_model(actual)
    return have == want or (not want.startswith("claude-") and want in have)


def norm_folder(path: str) -> str:
    return os.path.normpath(os.path.expanduser(path)) if path else ""


def folder_ok(expected: str, folders: list[str]) -> bool:
    want = norm_folder(expected)
    return any(f == want or f.startswith(want + os.sep) for f in map(norm_folder, folders) if f)


# ---------- guards ----------

def link(meta: dict) -> str:
    return f"[{meta['title'] or meta['sessionId']}](#{meta['sessionId']})"


def request_of(meta: dict, starts: dict) -> dict | None:
    by_session, by_task = starts
    spawned = meta.get("spawnedFrom") or {}
    return by_session.get(meta["sessionId"]) or by_task.get(spawned.get("taskId", ""))


def model_row(child: dict, ask: dict, orch: dict, tail: dict) -> tuple | None:
    actual = tail["model"] or child.get("model", "")
    expected = ask["model"] or (BUILD_FAMILY if ask["build"] else "")
    if not expected or not actual or model_ok(expected, actual):
        return None
    why = "" if ask["model"] else " (a build child with no model set)"
    text = (f"Child {link(child)} runs {actual}, but {link(orch)} asked for {expected}{why}. "
            "Stop it and start it again with the right model.")
    return ("alert", text)


def project_root(path: str) -> str:
    """A worktree under <repo>/.claude/worktrees/ belongs to <repo>. Its children get sibling worktrees."""
    marker = os.sep + os.path.join(".claude", "worktrees") + os.sep
    return path.split(marker, 1)[0] if marker in path else path


def folder_row(child: dict, ask: dict, orch: dict, tail: dict, now: float) -> tuple | None:
    expected = ask["folder"] or project_root(orch.get("originCwd") or orch.get("cwd", ""))
    if not expected or now - child.get("createdAt", 0) / 1000 < GRACE_S:
        return None
    folders = [tail["cwd"], child.get("cwd", ""), child.get("worktreePath", ""), child.get("originCwd", "")]
    if folder_ok(expected, folders):
        return None
    actual = tail["cwd"] or child.get("cwd", "") or "an unknown folder"
    text = (f"Child {link(child)} works in {actual}, but {link(orch)} asked for {expected}. "
            "Move it, or start it again in the right folder.")
    return ("alert", text)


def child_rows(records: dict, orchestrators: dict, starts: dict, paths, now: float, tails: tuple) -> list[tuple]:
    rows = []
    for meta in records.values():
        orch = orchestrators.get((meta.get("spawnedFrom") or {}).get("sessionId", ""))
        if not orch or meta["isArchived"] or now - meta.get("createdAt", 0) / 1000 > WINDOW_S:
            continue
        ask = request_of(meta, starts.get(orch["sessionId"], ({}, {})))
        if not ask:
            continue
        tail = transcript_tail(paths(meta), *tails)
        source = f"session record {meta['sessionId']}"
        for row in (model_row(meta, ask, orch, tail), folder_row(meta, ask, orch, tail, now)):
            if row:
                rows.append((*row, source, meta.get("createdAt", 0) / 1000))
    return rows


def stalled_rows(records: dict, orchestrators: dict, now: float, clock) -> list[tuple]:
    """Children idle more than a day with work not finished. Open PRs already have their own fact."""
    rows = []
    for meta in records.values():
        if (meta.get("spawnedFrom") or {}).get("sessionId") not in orchestrators or meta["isArchived"] \
                or meta["scheduledTaskId"] or any(p["state"] == "OPEN" for p in meta["prs"]):
            continue
        idle = now - meta["lastActivityAt"] / 1000
        status = meta["postTurnSummary"].get("status_category") or "no status"
        asking = status == "blocked" and meta["postTurnSummary"].get("needs_action")  # listed as a question
        if not STALL_S < idle <= STALL_WINDOW_S or status == "completed" or asking:
            continue
        text = (f"{link(meta)} idle since {clock(meta['lastActivityAt'] / 1000)}, status {status}, "
                "no open PR. Move it on or close it.")
        rows.append(("idle_pr", text, f"session record {meta['sessionId']}", meta["lastActivityAt"] / 1000))
    return rows


def murmur_rows(scan: dict, meta: dict, now: float, clock) -> list[tuple]:
    """A murmur is not a decision. A relay, merge or approval right after one is flagged."""
    rows = []
    for turn in scan.get("turns", []):
        if now - turn["at"] > MURMUR_WINDOW_S or not MURMUR_RE.match(turn["text"].strip()):
            continue
        used = [s.split(" ", 1)[0].split("__")[-1] for s in turn["tools"]
                if any(word in s.lower() for word in DECISION_TOOLS)]
        if used:
            text = (f"{link(meta)} took \"{turn['text'][:20]}\" at {clock(turn['at'])} as a decision "
                    f"({', '.join(dict.fromkeys(used))}). A murmur is not a decision: ask again.")
            rows.append(("alert", text, f"transcript {os.path.basename(scan.get('path', ''))}", turn["at"]))
    return rows


def rows(records: dict, orchestrators: dict, scans: dict, cache: dict, now: float, find_transcript, clock) -> list[tuple]:
    """Every guard row. `cache` is the guards' own part of the build cache."""
    old, fresh, starts = cache.get("starts", {}), {}, {}
    for local_id, meta in orchestrators.items():
        path = find_transcript(meta)
        if not path:
            continue
        fresh[local_id] = scan_starts(path, old.get(local_id))
        items = fresh[local_id]["starts"]
        starts[local_id] = ({s["session_id"]: s for s in items if s["session_id"]},
                            {s["task_id"]: s for s in items if s["task_id"]})
    cache["starts"] = fresh
    tails: dict = {}
    out = child_rows(records, orchestrators, starts, find_transcript, now, (cache.get("tails", {}), tails))
    cache["tails"] = tails  # only the children checked in this build
    out += stalled_rows(records, orchestrators, now, clock)
    for local_id, scan in scans.items():
        out += murmur_rows(scan, orchestrators[local_id], now, clock)
    return out

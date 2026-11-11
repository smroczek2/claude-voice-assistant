"""The voice orchestrator's memory: one entry per task it started or watches.

Each entry keeps a spoken nickname, what the task is, why the user started it,
where it stands, the open question (with its choices and recommendation),
and the user's last words on it. The name used in output comes from
`user_name` in config.json (default "the user").

Facts come from two places, in this order of trust:
1. The session's own transcript file. Unlike list_events, it keeps the full
   tool arguments, so the exact wrap_up ask and AskUserQuestion choices are
   readable from outside the session.
2. The Desktop app's metadata for the session: its post-turn summary
   (status and needed action), PRs, and which session started it.
A field already saved in ledger.json (what, why, started_with, standing) is
kept when the sources no longer give it, so older entries keep the text that
the retired Orch mod once supplied.

Fields that Claude writes with `note` (what, why, nickname, said) are never
overwritten by `refresh`.

Commands (all print plain text for the voice session to read, then say in
its own words):
  refresh [--days N]        update every watched session
  waiting                   every task that needs the user, with its brief
  snapshot                  concise local state for every waiting task (paging is opt-in)
  where <words> [--compact] one full task, or a bounded local excerpt
  list                      every watched task, one line each
  watch <words|local_id>    start watching a session
  unwatch <words>           stop watching it
  note <words> [--what ..] [--why ..] [--nickname ..] [--alias ..] [--said ..] [--interest]
  brief <local_id> --as-of T  rebuild a past question from the transcript only (blind tests)
  orchestrators             sessions that run other sessions, plus named ones in config.json
  state --session ID [--budget turn|start]  the orchestrator digest (see orch_hook.py)
  follow [--interval S]     refresh forever; print one line when a task newly
                            needs the user, or a task they asked about finishes or fails
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import glob
import json
import os
import re
import sys
import time

from session_index import load_session_index, missing_sources, parse_iso, transcript_path

HOME = os.path.expanduser(os.environ.get("VOZ_STATE_DIR") or "~/.claude/voice-orchestrator")
LEDGER_PATH = os.path.join(HOME, "ledger.json")
CONFIG_PATH = os.path.join(HOME, "config.json")
VOICE_STORE_DIR = os.path.expanduser("~/.claude/plugins/store")
USER_FROM = "user"  # marks a message the user typed, as opposed to a relay from another session
SCHEMA = 3  # raise when entry fields change, so refresh rebuilds every cached entry
DEFAULT_CONFIG = {
    "watch_days": 3,
    "orchestrators": {},
    "routine_titles": [],
    "user_name": "the user",
    "voice_title": "Voice Orchestrator",
}
STOPWORDS = {"the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "fix", "issue",
             "add", "build", "make", "from", "into", "all", "my", "this", "that", "is", "by", "be"}
RECOMMEND_RE = re.compile(r"[^.\n]*\brecommend\w*\b[^.\n]*[.\n]?", re.IGNORECASE)
NOISE_PREFIXES = ("<command-", "<local-command", "<task-notification", "<system-reminder", "Caveat:",
                  "[Request interrupted", "<side-sessions-event", "<ci-monitor-event",
                  "This session is being continued", "<bash-input", "<bash-stdout",
                  "<artifact-content-authored-by-others")


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int | None) -> str:
    if not ms:
        return ""
    return dt.datetime.fromtimestamp(ms / 1000).astimezone().strftime("%a %b %d, %I:%M %p")


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as error:
        sys.exit(f"{path} is not valid JSON ({error}). Fix or delete it, then run again.")


def save_ledger(ledger: dict) -> None:
    os.makedirs(HOME, exist_ok=True)
    temp = LEDGER_PATH + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(ledger, handle, indent=1)
    os.replace(temp, LEDGER_PATH)


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    config.update(load_json(CONFIG_PATH, {}))
    return config


def user_name() -> str:
    return str(load_config().get("user_name") or "the user")


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def possessive() -> str:
    name = cap(user_name())
    return name + ("'" if name.endswith("s") else "'s")


# ---------- transcript reading ----------

def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_tool_result(content) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def clean(text: str) -> str:
    text = re.sub(r"<pasted_content[^>]*>", "(pasted text:) ", text)
    text = re.sub(r"</pasted_content[^>]*>", " (end of paste)", text)
    return re.sub(r"<widget_context_hint>.*?</widget_context_hint>", "", text, flags=re.DOTALL).strip()


def person_message(entry: dict) -> dict | None:
    """A message the user typed, or one another session sent on their behalf."""
    content = (entry.get("message") or {}).get("content")
    if entry.get("type") != "user" or is_tool_result(content):
        return None
    origin = entry.get("origin") or {}
    if origin.get("kind") == "peer":
        body = origin.get("body") or text_of(content)
        if body.lstrip().startswith("[Subagent hand-back]"):
            return None
        return {"text": clean(body), "from": origin.get("name", "another session"), "at": entry.get("timestamp", "")}
    text = clean(text_of(content).strip())
    if text.startswith("[Subagent hand-back]"):
        return None
    if entry.get("isMeta") or not text or text.startswith(NOISE_PREFIXES) or origin.get("kind") not in (None, "human"):
        return None
    return {"text": text, "from": USER_FROM, "at": entry.get("timestamp", "")}


def ask_from_tool(block: dict, entry: dict, last_text: str) -> dict | None:
    name, args = block.get("name"), block.get("input") or {}
    if name == "AskUserQuestion":
        questions = args.get("questions") or []
        return {"text": " | ".join(q.get("question", "") for q in questions), "recap": "", "brief": last_text,
                "choices": [{"label": o.get("label", ""), "means": o.get("description", "")}
                            for q in questions for o in q.get("options") or []],
                "at": entry.get("timestamp", ""), "tool_id": block.get("id", "")}
    if name == "mcp__ccd_turn__wrap_up" and args.get("ask"):
        return {"text": args["ask"], "recap": args.get("recap", ""), "brief": "",
                "choices": [{"label": a.get("label") or a.get("action", ""), "means": ""}
                            for a in args.get("actions") or []],
                "at": entry.get("timestamp", ""), "tool_id": ""}
    return None


def scan_transcript(path: str, as_of: str = "") -> dict:
    """One pass over a transcript: first prompt, last words, open question, last answer.

    With as_of (an ISO time), stop just before that moment, to rebuild what a
    past question looked like before the user answered it.
    """
    facts = {"first": None, "last_user": None, "last_relay": None, "question": None, "closing": ""}
    last_text, answered_tools = "", set()
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if as_of and entry.get("timestamp", "") >= as_of:
                break
            content = (entry.get("message") or {}).get("content")
            if entry.get("type") == "assistant" and isinstance(content, list):
                last_text = scan_assistant(content, entry, facts, last_text)
                continue
            if is_tool_result(content):
                answered_tools.update(b.get("tool_use_id") for b in content if isinstance(b, dict))
            message = person_message(entry)
            if not message:
                continue
            facts["first"] = facts["first"] or message
            facts["last_user" if message["from"] == USER_FROM else "last_relay"] = message
            facts["question"] = None
    question = facts["question"]
    if question and question["tool_id"] in answered_tools:
        facts["question"] = None
    return facts


def scan_assistant(content: list, entry: dict, facts: dict, last_text: str) -> str:
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text", "").strip():
            last_text = block["text"].strip()
            facts["closing"] = last_text
            if facts["question"] and facts["question"]["recap"] is not None and not facts["question"]["brief"]:
                facts["question"]["brief"] = last_text
        elif block.get("type") == "tool_use":
            ask = ask_from_tool(block, entry, last_text)
            if ask:
                facts["question"] = ask
    return last_text


# ---------- entry building ----------

def speakable_nickname(title: str, taken: set[str]) -> str:
    words = re.sub(r"'s\b|[#:()\[\]/\"'`]", " ", title).split()
    words = [w for w in words if not w.isdigit() and w.lower() not in STOPWORDS]
    base = " ".join(words[:3]).lower() or "untitled"
    nickname, n = base, 2
    while nickname in taken:
        nickname, n = f"{base} {n}", n + 1
    return nickname


def aliases_from(meta: dict) -> list[str]:
    found = set(re.findall(r"#?(\d{3,5})", meta.get("title", "")))
    for pr in meta.get("prs") or []:
        if pr.get("prNumber"):
            found.add(str(pr["prNumber"]))
    if meta.get("prNumber"):
        found.add(str(meta["prNumber"]))
    return sorted(found)


def recommendation(question: dict | None) -> str:
    if not question:
        return ""
    for choice in question["choices"]:
        if "recommend" in choice["label"].lower():
            return choice["label"]
    match = RECOMMEND_RE.search(question.get("brief", "") + "\n" + question.get("recap", ""))
    return match.group(0).strip() if match else ""


def status_of(meta: dict, facts: dict, question: dict | None) -> str:
    summary = meta.get("postTurnSummary") or {}
    if meta.get("isArchived"):
        return "archived"
    if question:
        return "needs you"
    error = (meta.get("priorErrorMark") or {}).get("at", 0)
    if error and error >= meta.get("lastTurnEdgeAt", 0) - 1000:
        return "failed"
    if meta.get("lastTurnStopped"):
        return "stopped"
    if summary.get("status_category") == "blocked" and summary.get("needs_action"):
        return "needs you"
    if summary.get("status_category") == "review_ready":
        return "ready for review"
    if summary.get("status_category") == "completed":
        return "finished"
    return "idle"


def build_entry(meta: dict, facts: dict, old: dict, taken: set[str]) -> dict:
    summary = meta.get("postTurnSummary") or {}
    question = facts["question"]
    if question:
        question = dict(question, recommendation=recommendation(question))
        question.pop("tool_id", None)
    first = facts["first"] or {}
    spawned = meta.get("spawnedFrom") or {}
    entry = {
        "session_id": meta["sessionId"],
        "title": meta.get("title", ""),
        "nickname": old.get("nickname") or speakable_nickname(meta.get("title", ""), taken),
        "aliases": sorted(set(old.get("aliases", [])) | set(aliases_from(meta))),
        "what": old.get("what", ""),
        "why": old.get("why", ""),
        "started_with": (old.get("started_with") or first.get("text", ""))[:1500],
        "started_by": spawned.get("sessionId") or meta.get("parentSessionId") or "",
        "standing": facts["closing"][:900] or old.get("standing", ""),
        "app_summary": summary.get("status_detail", ""),
        "needs_action": summary.get("needs_action", ""),
        "last_answer": facts["closing"][:1500],
        "question": question,
        "last_words": facts["last_user"],
        "last_relay": facts["last_relay"],
        "said": old.get("said", []),
        "prs": [{"number": p.get("prNumber"), "repo": p.get("repo")} for p in meta.get("prs") or []],
        "last_activity": meta.get("lastActivityAt", 0),
        "watched": old.get("watched", True),
        "interest": old.get("interest", False),
        "transcript_mtime": facts.get("mtime", 0),
    }
    entry["status"] = status_of(meta, facts, question)
    return entry


# ---------- refresh ----------

def wanted(meta: dict, old: dict | None, config: dict, cutoff_ms: int) -> bool:
    if old is not None:
        return old.get("watched", True)
    title = meta.get("title", "").lower()
    if meta.get("isArchived") or meta.get("scheduledTaskId") or title == str(config["voice_title"]).lower():
        return False
    if any(pattern in title for pattern in config["routine_titles"]):
        return False
    return meta.get("isStarred") or meta.get("lastActivityAt", 0) >= cutoff_ms


def refresh(ledger: dict, days: int | None = None) -> dict:
    config = load_config()
    cutoff = now_ms() - int((days or config["watch_days"]) * 86400_000)
    index = load_session_index()
    entries = ledger.setdefault("entries", {})
    taken = {e["nickname"] for e in entries.values()}
    for local_id, meta in index.by_local.items():
        old = entries.get(local_id)
        if not wanted(meta, old, config, cutoff):
            continue
        path = transcript_path(meta)
        mtime = int(os.path.getmtime(path) * 1000) if path else 0
        cached = ledger.get("schema") == SCHEMA and old and old.get("transcript_mtime") == mtime
        if cached and old.get("last_activity") == meta.get("lastActivityAt", 0):
            continue
        facts = scan_transcript(path) if path else {"first": None, "last_user": None, "last_relay": None,
                                                     "question": None, "closing": ""}
        facts["mtime"] = mtime
        entry = build_entry(meta, facts, old or {}, taken)
        taken.add(entry["nickname"])
        entries[local_id] = entry
    ledger["refreshed_at"] = now_ms()
    ledger["schema"] = SCHEMA
    save_ledger(ledger)
    return ledger


# ---------- lookup and printing ----------

def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS}


def score(query: str, entry: dict) -> float:
    q = tokens(query)
    if not q:
        return 0.0
    names = [entry["nickname"], *entry["aliases"]]
    if query.lower().strip() in names:
        return 10.0
    hay = tokens(" ".join([entry["nickname"], entry["title"], entry.get("what", ""), *entry["aliases"]]))
    overlap = len(q & hay) / len(q)
    fuzzy = max(difflib.SequenceMatcher(None, query.lower(), n).ratio() for n in names)
    return overlap * 2 + fuzzy


def find(ledger: dict, query: str) -> list[dict]:
    entries = list(ledger.get("entries", {}).values())
    if query in ledger.get("entries", {}):
        return [ledger["entries"][query]]
    ranked = sorted(((score(query, e), e) for e in entries), key=lambda pair: -pair[0])
    ranked = [(s, e) for s, e in ranked if s >= 0.9]
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.3:
        return [e for _, e in ranked[:3]]
    return [e for _, e in ranked[:1]]


def words_line(label: str, message: dict | None, limit: int | None = 500) -> str:
    if not message:
        return ""
    parsed = parse_iso(message.get("at", ""))
    when = iso(int(parsed.timestamp() * 1000)) if parsed else ""
    return f"{label} ({message['from']}, {when}): {message['text'][:limit]}"


def describe(entry: dict, full: bool) -> str:
    lines = [f"== {entry['nickname']}  [{entry['status']}]  (title: {entry['title']})",
             f"What it is: {entry.get('what') or 'NOT WRITTEN YET - read started_with and write it with note --what'}",
             f"Why it exists: {entry.get('why') or 'NOT WRITTEN YET - infer from started_with, then save with note --why'}",
             f"Where it stands (the session's own latest report): {entry['standing']}"]
    if entry.get("app_summary") or entry.get("needs_action"):
        lines.append(f"App's one-line status (a hint; it has been wrong, trust the report above): "
                     f"{entry.get('app_summary', '')} / needs: {entry.get('needs_action') or 'nothing'}")
    question = entry.get("question")
    if question:
        lines.append(f"Open question: {question['text']}")
        for choice in question["choices"]:
            lines.append(f"  choice: {choice['label']}" + (f" - {choice['means']}" if choice["means"] else ""))
        if question.get("recommendation"):
            lines.append(f"  session recommends: {question['recommendation']}")
        if full:
            lines.append(f"  recap: {question.get('recap', '')}")
            lines.append(f"  brief: {question.get('brief', '')}")
    for said in entry.get("said", [])[-2:]:
        lines.append(f"{cap(user_name())} said to voice ({iso(said['at'])}): {said['text']}")
    lines.extend(filter(None, [words_line(f"{possessive()} last words in the session", entry.get("last_words"), None if full else 500),
                               words_line("Last relayed message", entry.get("last_relay"), None if full else 500)]))
    if entry["prs"]:
        lines.append("PRs: " + ", ".join(f"{p['repo']} {p['number']}" for p in entry["prs"]))
    lines.append(f"Last activity: {iso(entry['last_activity'])}. Session id (never say it): {entry['session_id']}")
    if full:
        lines.append(f"Started with: {entry['started_with']}")
        lines.append(f"Last answer from the session: {entry['last_answer']}")
    return "\n".join(lines)


def compact_entry(entry: dict) -> dict:
    """Concise current-state projection without cutting decision text or choices.

    Historical background remains in the full detail command. Current questions,
    choices and latest directions stay intact. Task coverage is never limited.
    """
    item = {"session_id": entry["session_id"], "title": entry.get("title", ""),
            "status": entry["status"], "last_activity": entry.get("last_activity", 0),
            "truncated_fields": []}
    for key in ("nickname", "what", "needs_action"):
        value = entry.get(key)
        if value and value != item["title"]:
            item[key] = value
    question = entry.get("question")
    if question:
        item["question"] = {key: question[key] for key in ("text", "choices", "at", "recommendation") if key in question}
        item["question"]["choice_count"] = len(question.get("choices") or [])
        if question.get("recap") or question.get("brief"):
            item["background_in_full_detail"] = True
        if item.get("needs_action") == question.get("text"):
            item.pop("needs_action")
    standing = entry.get("standing", "")
    represented = [item["question"].get("text", "")] if question else []
    if standing and not any(text.startswith(standing) for text in represented if isinstance(text, str)):
        item["standing"] = standing
    if entry.get("last_words"):
        item["last_words"] = entry["last_words"]
    if entry.get("said"):
        item["said"] = entry["said"][-1]
    return item


def snapshot(ledger: dict, entries: list[dict], limit: int | None = None, offset: int = 0) -> dict:
    ordered = sorted(entries, key=lambda entry: (-entry.get("last_activity", 0), entry["session_id"]))
    end = None if limit is None else offset + limit
    return {"source": "local session transcripts; verify the selected live source before acting",
            "refreshed_at": ledger.get("refreshed_at"), "total": len(ordered), "offset": offset,
            "limit": limit, "has_more": end is not None and end < len(ordered),
            "detail": "where <session_id> includes complete task background and reports",
            "items": [compact_entry(entry) for entry in ordered[offset:end]]}


def integer_range(low: int, high: int | None = None):
    def parse(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("Value must be an integer in range.")
        if number < low or (high is not None and number > high):
            raise argparse.ArgumentTypeError(f"Value outside range {low}..{high if high is not None else 'unlimited'}.")
        return number
    return parse


# ---------- follow (events) ----------

def voice_session_ids() -> set[str]:
    """Resolve voice ownership by identity even when the user renames its chat.

    Only notification output excludes these IDs; all task views retain coverage.
    Read each poll because ownership may transfer to another conversation.
    """
    owners = set()
    for path in glob.glob(os.path.join(VOICE_STORE_DIR, "voz*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                store = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        owner = store.get("voz.assistant") if isinstance(store, dict) else None
        if isinstance(owner, str) and owner:
            owners.add(owner)
    if owners:
        index = load_session_index()
        for owner in list(owners):
            local_id = (index.by_cli.get(owner) or {}).get("sessionId")
            if local_id:
                owners.add(local_id)
    return owners


def events_between(before: dict, after: dict, excluded: set[str] | None = None) -> list[str]:
    out = []
    for local_id, entry in after.get("entries", {}).items():
        if local_id in (excluded or ()):
            continue
        prior = before.get("entries", {}).get(local_id, {})
        was, now = prior.get("status"), entry["status"]
        if now == "needs you" and (was != "needs you" or (entry.get("question") or {}).get("at") != (prior.get("question") or {}).get("at")):
            out.append(f"NEEDS_YOU {entry['nickname']}: {(entry.get('question') or {}).get('text') or entry.get('needs_action')}")
        elif entry.get("interest") and now in ("finished", "ready for review", "failed", "stopped") and was != now:
            out.append(f"{now.upper().replace(' ', '_')} {entry['nickname']}: {entry['standing'][:200]}")
    return out


def follow(interval: int) -> None:
    ledger = refresh(load_json(LEDGER_PATH, {}))
    while True:
        time.sleep(interval)
        before = json.loads(json.dumps(ledger))
        try:
            ledger = refresh(ledger)
        except OSError as error:
            print(f"REFRESH_ERROR {error}", flush=True)
            continue
        for line in events_between(before, ledger, voice_session_ids()):
            print(line, flush=True)


# ---------- commands ----------

def one(ledger: dict, query: str) -> dict:
    matches = find(ledger, query)
    if not matches:
        sys.exit(f"No watched task matches '{query}'. Run `ledger.py list`, or `ledger.py watch <words>` to add one.")
    if len(matches) > 1:
        options = "\n".join(f"- {m['nickname']}: {m['title']}" for m in matches)
        sys.exit(f"More than one task matches. Ask {user_name()} which one:\n{options}")
    return matches[0]


def cmd_note(ledger: dict, args) -> None:
    entry = one(ledger, args.query)
    for field in ("what", "why", "nickname"):
        value = getattr(args, field)
        if value:
            entry[field] = value.strip()
    if args.alias:
        entry["aliases"] = sorted(set(entry["aliases"]) | {args.alias.lower()})
    if args.said:
        entry.setdefault("said", []).append({"text": args.said, "at": now_ms()})
    if args.interest:
        entry["interest"] = True
    save_ledger(ledger)
    print(describe(entry, full=False))


def cmd_watch(ledger: dict, args, on: bool) -> None:
    if on and args.query.startswith("local_"):
        ledger.setdefault("entries", {}).setdefault(args.query, {"watched": True, "transcript_mtime": -1,
                                                                  "nickname": "", "aliases": []})
        ledger["entries"][args.query].update(watched=True, transcript_mtime=-1)
        refresh(ledger)
        print(describe(ledger["entries"][args.query], full=False))
        return
    entry = one(ledger, args.query)
    entry["watched"] = on
    save_ledger(ledger)
    print(f"{'Watching' if on else 'Stopped watching'} {entry['nickname']}.")


def print_past_brief(local_id: str, as_of: str) -> None:
    """Transcript-only view of a session just before a past answer; app summaries are current, so skip them."""
    meta = load_session_index().by_local.get(local_id)
    path = transcript_path(meta) if meta else None
    if not path:
        sys.exit(f"No transcript found for {local_id}.")
    facts = scan_transcript(path, as_of)
    question = facts["question"]
    lines = [f"== {meta.get('title')} (as of {as_of})",
             f"Started with: {((facts['first'] or {}).get('text', ''))[:1500]}"]
    lines.extend(filter(None, [words_line(f"{possessive()} last words before this", facts["last_user"]),
                               words_line("Last relayed message", facts["last_relay"])]))
    if question:
        lines += [f"Open question: {question['text']}", f"recap: {question['recap']}",
                  f"brief: {question['brief'][:2500]}"]
        lines += [f"  choice: {c['label']}" + (f" - {c['means']}" if c["means"] else "") for c in question["choices"]]
    else:
        lines.append(f"No open question at that time. Last answer: {facts['closing'][:1500]}")
    print("\n".join(lines))


def print_orchestrators() -> None:
    """Sessions that started other sessions, and the ones the user named in config.json."""
    index = load_session_index()
    named = {local_id: name for name, local_id in load_config()["orchestrators"].items()}
    children: dict[str, int] = {}
    for meta in index.by_local.values():
        parent = (meta.get("spawnedFrom") or {}).get("sessionId")
        if parent and not meta.get("isArchived"):
            children[parent] = children.get(parent, 0) + 1
    rows = []
    for local_id in set(children) | set(named):
        meta = index.by_local.get(local_id)
        if not meta or meta.get("isArchived"):
            continue
        label = f" ({cap(user_name())} calls it: {named[local_id]})" if local_id in named else ""
        line = (f"- {meta.get('title')}{label}: {children.get(local_id, 0)} open child sessions, "
                f"last active {iso(meta.get('lastActivityAt'))}. id: {local_id}")
        rows.append((meta.get("lastActivityAt", 0), line))
    print("\n".join(row for _, row in sorted(rows, reverse=True)) or "No orchestrator sessions found.")


def print_waiting(watched: list[dict]) -> None:
    waiting = sorted((e for e in watched if e["status"] == "needs you"), key=lambda e: -e["last_activity"])
    week_ago = now_ms() - 7 * 86400_000
    fresh = [e for e in waiting if e["last_activity"] >= week_ago]
    old = [e for e in waiting if e["last_activity"] < week_ago]
    print(f"{len(fresh)} task(s) wait on {user_name()} from this week; {len(old)} older open question(s) in pinned sessions.")
    for entry in fresh:
        print("\n" + describe(entry, full=False))
    if old:
        print("\nOlder, probably settled elsewhere (mention only if asked):")
        for entry in old:
            print(f"- {entry['nickname']}: {(entry.get('question') or {}).get('text') or entry.get('needs_action')} ({iso(entry['last_activity'])})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Voice orchestrator ledger")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("refresh").add_argument("--days", type=float)
    sub.add_parser("waiting")
    sub.add_parser("list")
    sub.add_parser("orchestrators")
    compact = sub.add_parser("snapshot")
    compact.add_argument("--status", choices=["waiting", "all"], default="waiting")
    compact.add_argument("--limit", type=integer_range(1), default=None)
    compact.add_argument("--offset", type=integer_range(0), default=0)
    past = sub.add_parser("brief")
    past.add_argument("session_id")
    past.add_argument("--as-of", required=True)
    for name in ("where", "watch", "unwatch"):
        command = sub.add_parser(name)
        command.add_argument("query", nargs="+")
        if name == "where":
            command.add_argument("--compact", action="store_true")
    note = sub.add_parser("note")
    note.add_argument("query", nargs="+")
    for flag in ("--what", "--why", "--nickname", "--alias", "--said"):
        note.add_argument(flag)
    note.add_argument("--interest", action="store_true")
    sub.add_parser("follow").add_argument("--interval", type=int, default=60)
    state = sub.add_parser("state")
    state.add_argument("--session", required=True)
    state.add_argument("--budget", choices=["turn", "start"], default="turn")
    state.add_argument("--limit", type=float, default=None)
    args = parser.parse_args()
    if hasattr(args, "query"):
        args.query = " ".join(args.query)
    for path in missing_sources():
        print(f"Note: {path} does not exist on this Mac, so no sessions from it are listed. "
              "Set VOZ_DESKTOP_SESSIONS_DIR or CLAUDE_CONFIG_DIR if your records live elsewhere.", file=sys.stderr)
    if args.command == "state":
        import orch_hook
        text = orch_hook.output(args.budget, args.session, args.limit)
        if text:
            print(text)
        return 0
    if args.command == "brief":
        print_past_brief(args.session_id, args.as_of)
        return 0
    if args.command == "orchestrators":
        print_orchestrators()
        return 0
    if args.command == "follow":
        follow(args.interval)
        return 0
    ledger = load_json(LEDGER_PATH, {})
    if args.command in ("refresh", "waiting", "list", "where", "snapshot"):
        ledger = refresh(ledger, getattr(args, "days", None))
    watched = [e for e in ledger.get("entries", {}).values() if e.get("watched", True) and e.get("status") != "archived"]
    if args.command == "snapshot":
        selected = [e for e in watched if args.status == "all" or e["status"] == "needs you"]
        print(json.dumps(snapshot(ledger, selected, args.limit, args.offset), ensure_ascii=False, separators=(",", ":")))
    elif args.command == "refresh":
        print(f"{len(watched)} watched tasks; {sum(e['status'] == 'needs you' for e in watched)} need {user_name()}.")
    elif args.command == "waiting":
        print_waiting(watched)
    elif args.command == "list":
        for e in sorted(watched, key=lambda e: -e["last_activity"]):
            print(f"- {e['nickname']} [{e['status']}] {e['title']} ({iso(e['last_activity'])})")
    elif args.command == "where":
        entry = one(ledger, args.query)
        if args.compact:
            print(json.dumps(snapshot(ledger, [entry]), ensure_ascii=False, separators=(",", ":")))
        else:
            print(describe(entry, full=True))
    elif args.command == "note":
        cmd_note(ledger, args)
    else:
        cmd_watch(ledger, args, on=args.command == "watch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

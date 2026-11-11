"""Mine the user's decisions and corrections from the orchestrator transcripts, by code.

No model call. The miners read only the lines added since the last run and write
two append-only logs in the state folder (`~/.claude/orchestrator/`, or `VOZ_ORCH_DIR`):

  decisions.jsonl        the user's words that approve, reject or choose, with the
                         question before them, the session, the time and the source
  rule-candidates.jsonl  the user's corrections ("you forgot", "I told you", "stop"),
                         each a candidate for a line in rules.md

Only the user's own turns count: typed turns, spoken turns that the voice plugin
submits as the user, and answers to a question tool. A message that another
session relays (a voice aside sent on) is not the user's turn. A murmur such as
"Mm-hmm" is never a decision. A bare "yes" or "no" is a decision only when a
question came before it.

The state builder (`orch_state.py build`) runs the miners under its lock and puts
the newest decisions in the start digest. The writer is the same: one lock and an
atomic rename for each file.

Commands:
  mine                     mine now (waits for the state lock)
  decisions [--last N]     print the newest decisions
  candidates               print the rule candidates, grouped by their words
  import-rules --dry-run   print the lines an import would add to rules.md; writes nothing

Runs on the macOS system Python 3.9 with the standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger
import orch_state

DECISIONS_FILE = "decisions.jsonl"
CANDIDATES_FILE = "rule-candidates.jsonl"
# The cache file name carries its schema, so code with another schema never reads it.
MINE_SCHEMA = 1
MINE_CACHE = f"mine-cache-v{MINE_SCHEMA}.json"
FILE_KEYS = {"inode", "offset", "ask", "said"}
KEEP_IN_STATE = 10
WORDS_MAX = 300
QUESTION_MAX = 240
SHORT_QUESTION_WORDS = 6  # a shorter question ("Send?") keeps the sentence before it
CONTEXT_MAX = 120

_LEAD = r"^(?:(?:yes|yeah|yep|yup|ok(?:ay)?|sure|great|perfect|good|cool|all right|alright|fine)[,.!]?\s+)*(?:please,?\s+)?"
MURMUR = re.compile(r"^(?:m+-?hm+|m+hm+|h+m+|uh-?huh|uh+|um+|ah+|oh+|huh|er+|you)[.!?…]*$", re.IGNORECASE)
REJECT = re.compile(r"^(?:no|nah|nope)[,.!]?\s+(?:don'?t|do not|let'?s not|skip|stop|cancel|wait|leave)\b"
                    r"|^(?:don'?t|do not)\s+(?:merge|send|do|start|push|commit|post|open|archive)\b"
                    r"|^(?:skip|cancel|drop)\s+(?:it|that|this|them)\b|^hold off\b|^not now\b|^let'?s not\b", re.IGNORECASE)
APPROVE = re.compile(_LEAD + r"(?:go ahead|do (?:it|that|this|both|all of (?:it|them))|let'?s do (?:it|that|this|both)"
                     r"|ship it|send (?:it|that|them|this)|merge (?:it|that|them|both|this)|commit (?:it|that|this)"
                     r"|push (?:it|that|this)|post (?:it|that|this)|open (?:it|the pr)|apply(?: it)?|approved?|use (?:those|that|it|them|these|this)"
                     r"|start (?:it|that|them|this)|sounds good|that'?s (?:fine|okay|ok|good|great|right)|we'?re good to)\b",
                     re.IGNORECASE)
YES_VERB = re.compile(r"^(?:yes|yeah|yep|yup|ok(?:ay)?|sure)[,.!]?\s+(?:please,?\s+)?(?:go ahead and\s+)?"
                      r"(?:merge|send|start|do|build|write|file|archive|post|commit|push|open|apply|approve|use|run|fix"
                      r"|add|close|ship|go|make|create|update|check|find|stop|kill|delete|remove|record|tell)\b", re.IGNORECASE)
CHOOSE = re.compile(_LEAD + r"(?:let'?s go with|go with|pick|option\s+\w+|(?:the\s+)?(?:first|second|third|last|other) one"
                    r"|number\s+\w+|whatever is best|both)\b", re.IGNORECASE)
BARE_YES = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|correct|please|yes,? please|absolutely|definitely|do)$", re.IGNORECASE)
BARE_NO = re.compile(r"^(?:no|nah|nope|not yet|no thanks)$", re.IGNORECASE)
CORRECTIONS = [
    ("you forgot", r"\byou forgot\b"),
    ("I told you", r"\bi (?:already )?told you\b"),
    ("I already", r"\bi already (?:said|asked|told|did|clicked|answered|gave|sent|merged)\b"),
    ("why didn't you", r"\bwhy (?:didn'?t|did not|haven'?t|have not|aren'?t|are you not) you\b|\bwhy didn'?t you\b"),
    ("again", r"^again\b|\bagain,? (?:the|you|i|we)\b"),
    ("stop", r"^stop\b(?!\s+(?:the|that|this|it|them|session|server|recording)\b)|\bstop (?:doing|saying|asking|telling|repeating|making|using)\b"),
    ("you keep", r"\byou keep\b"),
    ("how many times", r"\bhow many times\b"),
    ("not what I asked", r"\bnot what i (?:asked|said|meant|wanted)\b"),
    ("never", r"\b(?:never|don'?t ever) (?:do|say|ask|send|start|use|post|merge|put|run)\b"),
    ("I said", r"^i said\b|\bi said (?:not to|to|no)\b"),
]
CORRECTION_RES = [(phrase, re.compile(pattern, re.IGNORECASE)) for phrase, pattern in CORRECTIONS]
QUESTION_RE = re.compile(r"[^.!?\n]*\?")
WH_WORDS = {"what", "where", "when", "why", "how", "who", "whom", "whose", "which"}
BARE_MAX_WORDS = 12  # a longer turn that starts with "yes" or "no" is talk, not a decision


class MineError(Exception):
    """A fault that stops the miners. The message says what to fix."""


# ---------- the user's turns ----------

def turn_of(entry: dict) -> dict | None:
    """A turn the user typed or spoke, as {"text", "channel"}. None for every other entry."""
    content = (entry.get("message") or {}).get("content")
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isCompactSummary") or ledger.is_tool_result(content):
        return None
    origin = entry.get("origin") or {}
    kind = origin.get("kind")
    if kind == "plugin" and origin.get("asUser"):
        channel = "spoken"
    elif kind in (None, "human"):
        channel = "typed"
    else:
        return None  # a relay from another session, a task notification, a plugin note
    text = ledger.clean(ledger.text_of(content).strip())
    if not text or text.startswith((*ledger.NOISE_PREFIXES, "[Subagent hand-back]")) or orch_state.is_injected(text):
        return None
    return {"text": text, "channel": channel}


def first_clause(text: str) -> str:
    return re.split(r"[,.!;:\n]", text.strip(), maxsplit=1)[0].strip()


def is_murmur(text: str) -> bool:
    return bool(MURMUR.match(text.strip())) or all(MURMUR.match(part) for part in re.split(r"[\s,]+", text.strip()) if part)


def classify(text: str, asked: str) -> str | None:
    """approve, reject or choose; None for words that settle nothing. `asked` is the question before, or ""."""
    words = text.strip()
    if not words or is_murmur(words):
        return None
    if words.endswith("?") and not re.search(r"[.!]\s", words):
        return None  # one sentence that asks: a request, not a decision
    for kind, pattern in (("reject", REJECT), ("approve", APPROVE), ("approve", YES_VERB), ("choose", CHOOSE)):
        if pattern.match(words):
            return kind
    if not asked or not yes_no_question(asked) or len(words.split()) > BARE_MAX_WORDS:
        return None
    clause = first_clause(words)
    if BARE_YES.match(clause):
        return "approve"
    if BARE_NO.match(clause):
        return "reject"
    return None


def yes_no_question(question: str) -> bool:
    """A question that "yes" or "no" can settle: not one that starts with what, where, why and the like."""
    body = re.sub(r"^[^:]{0,40}:\s*", "", question.strip(" \"'*"))
    first = re.split(r"\W+", body.lower().strip(), maxsplit=1)[0] if body.strip() else ""
    return first not in WH_WORDS


def corrections_in(text: str) -> list[str]:
    if "(pasted text:)" in text:
        return []  # pasted material is not the user's own words
    lowered = text.lower()
    return [phrase for phrase, pattern in CORRECTION_RES if pattern.search(lowered)]


# ---------- the question before a turn ----------

def question_in(text: str) -> tuple[str, str]:
    """The last question in the text, and the text before it."""
    found = list(QUESTION_RE.finditer(text))
    if not found:
        return "", ""
    last = found[-1]
    return last.group().strip(" \t\"'*`")[:QUESTION_MAX], text[:last.start()]


def with_context(question: str, before: str) -> str:
    """A short question with the sentence before it, so "Send?" says what was sent. Cut to 120 characters."""
    if len(question.split()) >= SHORT_QUESTION_WORDS:
        return question
    sentences = [part.strip(" \t\"'*`>-") for part in re.findall(r"[^.!?\n]+[.!?]?", before)]
    lead = next((part for part in reversed(sentences) if re.search(r"[A-Za-z]", part)), "")
    if not lead:
        return question
    joined = f"{lead} {question}"
    return joined if len(joined) <= CONTEXT_MAX else "…" + joined[-(CONTEXT_MAX - 1):]


def assistant_ask(entry: dict, scan: dict) -> None:
    """Keep the newest question the assistant asked: a question tool, a wrap-up ask, or a sentence with "?"."""
    for block in (entry.get("message") or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text", "").strip():
            scan["said"] = block["text"].strip()[-QUESTION_MAX:]
            asked, before = question_in(block["text"])
            if asked:
                scan["ask"], scan["ask_context"] = asked, with_context(asked, before)
        elif block.get("type") == "tool_use":
            tool_ask = ledger.ask_from_tool(block, entry, "")
            if tool_ask and tool_ask["text"]:
                scan["ask"] = tool_ask["text"][:QUESTION_MAX]
                scan["ask_context"] = with_context(scan["ask"], scan.get("said", ""))


# ---------- records ----------

def record_id(session: str, entry: dict, offset: int, extra: str = "") -> str:
    key = entry.get("uuid") or f"{offset}:{entry.get('timestamp', '')}"
    return hashlib.sha256(f"{session}:{key}:{extra}".encode()).hexdigest()[:16]


def decision(kind: str, words: str, question: str, channel: str, where: dict) -> dict:
    return {"id": where["id"], "at": where["at"], "kind": kind, "words": words[:WORDS_MAX], "question": question,
            "channel": channel, "session": where["session"], "source": where["source"]}


def answered_questions(entry: dict, where: dict) -> list[dict]:
    """Answers the user picked or typed in a question tool. Each is a choice."""
    result = entry.get("toolUseResult")
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict):
        return []
    out = []
    for index, (question, answer) in enumerate(answers.items()):
        words = ", ".join(answer) if isinstance(answer, list) else str(answer)
        if words.strip() and not words.lstrip().startswith("["):  # "[User did not answer ...]" is no choice
            item_where = {**where, "id": hashlib.sha256(f"{where['id']}:{index}".encode()).hexdigest()[:16]}
            out.append(decision("choose", words, str(question)[:QUESTION_MAX], "question tool", item_where))
    return out


def mine_entry(entry: dict, scan: dict, where: dict, out: dict) -> None:
    """Add the decisions and rule candidates of one transcript line to `out`."""
    if entry.get("type") == "assistant":
        assistant_ask(entry, scan)
        return
    answers = answered_questions(entry, where)
    if answers:
        out["decisions"].extend(answers)
        scan["ask"], scan["ask_context"] = "", ""
        return
    turn = turn_of(entry)
    if not turn:
        return
    text, asked = turn["text"], scan.get("ask", "")
    kind = classify(text, asked)
    if kind:
        out["decisions"].append(decision(kind, text, scan.get("ask_context") or asked, turn["channel"], where))
    phrases = corrections_in(text)
    if phrases:
        out["candidates"].append({"id": where["id"], "at": where["at"], "phrases": phrases, "words": text[:WORDS_MAX],
                                  "after": scan.get("said", "")[-QUESTION_MAX:], "channel": turn["channel"],
                                  "session": where["session"], "source": where["source"]})
    if not is_murmur(text):
        scan["ask"], scan["ask_context"] = "", ""  # the turn answered the question, or moved on from it


# ---------- transcripts (read from the last offset) ----------

def scan_file(session: str, path: str, prior: dict | None, out: dict) -> dict:
    """Read only the lines added since the last run. A replaced or shorter file is read again."""
    info = os.stat(path)
    fresh = {"inode": info.st_ino, "offset": 0, "ask": "", "said": ""}
    scan = prior if isinstance(prior, dict) and FILE_KEYS <= set(prior) and prior.get("inode") == info.st_ino \
        and prior.get("offset", 0) <= info.st_size else fresh
    with open(path, "rb") as handle:
        handle.seek(scan["offset"])
        data = handle.read()
    end, position = data.rfind(b"\n") + 1, scan["offset"]
    for raw in data[:end].split(b"\n"):
        line_offset, position = position, position + len(raw) + 1
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            where = {"id": record_id(session, entry, line_offset), "at": orch_state.epoch(entry.get("timestamp", "")),
                     "session": session, "source": f"transcript {os.path.basename(path)}"}
            mine_entry(entry, scan, where, out)
    scan["offset"] += end
    return scan


def load_mine_cache() -> dict:
    try:
        cache = orch_state.read_json(orch_state.path_of(MINE_CACHE), {})
    except json.JSONDecodeError:
        return {}
    if not isinstance(cache, dict) or cache.get("schema") != MINE_SCHEMA or not isinstance(cache.get("files"), dict):
        return {}
    return cache


def read_log(name: str) -> list[dict]:
    """Every record in one log. A broken line stops the miners: the log is the user's record, not a cache."""
    path = orch_state.path_of(name)
    if not os.path.exists(path):
        return []
    items = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise MineError(f"{path} line {number} is not valid JSON ({error}). Fix or remove that line.") from error
    return items


def append_log(name: str, new: list[dict]) -> list[dict]:
    """Add the records not yet in the log, then replace the file with a rename. Returns the whole log."""
    items = read_log(name)
    known, added = {item.get("id") for item in items}, []
    for item in new:
        if item["id"] not in known:
            known.add(item["id"])
            added.append(item)
    if added:
        items.extend(added)
        text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items)
        orch_state.write_atomic(orch_state.path_of(name), text)
    return items


def mine(targets: dict) -> list[dict]:
    """Mine each {local_id: transcript path}. The caller holds the state lock. Returns the newest decisions."""
    cache = load_mine_cache()
    old_files, files = cache.get("files", {}), {}
    out = {"decisions": [], "candidates": []}
    for session, path in sorted(targets.items()):
        if path and os.path.exists(path):
            files[path] = scan_file(session, path, old_files.get(path), out)
    decisions = append_log(DECISIONS_FILE, out["decisions"])
    append_log(CANDIDATES_FILE, out["candidates"])
    orch_state.write_atomic(orch_state.path_of(MINE_CACHE), json.dumps({"schema": MINE_SCHEMA, "files": files}))
    newest = sorted(decisions, key=lambda item: item.get("at", 0))[-KEEP_IN_STATE:]
    return [{key: item.get(key, "") for key in ("at", "kind", "words", "question", "channel", "session")} for item in newest]


# ---------- rules.md import (dry run only) ----------

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip(" .!")


def import_lines(candidates: list[dict], rules_text: str) -> list[str]:
    """One dated line per candidate group, newest first, minus the words rules.md already holds."""
    groups: dict[str, list[dict]] = {}
    for item in candidates:
        groups.setdefault(normalize(item.get("words", "")), []).append(item)
    have = normalize(rules_text)
    lines = []
    for key, items in sorted(groups.items(), key=lambda pair: -max(i.get("at", 0) for i in pair[1])):
        if not key or key[:120] in have:
            continue
        newest = max(items, key=lambda i: i.get("at", 0))
        day = dt.datetime.fromtimestamp(newest.get("at", 0)).strftime("%Y-%m-%d") if newest.get("at") else "undated"
        phrases = sorted({p for i in items for p in i.get("phrases", [])})
        seen = f"; seen {len(items)} times" if len(items) > 1 else ""
        lines.append(f"- {day}: \"{newest['words'][:200]}\" ({', '.join(phrases)}; {newest.get('channel', '')}, "
                     f"{newest.get('session', '')}{seen})")
    return lines


def dry_run() -> int:
    target = orch_state.path_of("rules.md")
    rules_text = ""
    if os.path.exists(target):
        with open(target, encoding="utf-8") as handle:
            rules_text = handle.read()
    lines = import_lines(read_log(CANDIDATES_FILE), rules_text)
    print(f"Dry run: nothing written. An import would add {len(lines)} line(s) to {target}:")
    print("\n".join(lines) if lines else "(none)")
    return 0


# ---------- commands ----------

@contextlib.contextmanager
def state_lock():
    os.makedirs(orch_state.state_dir(), mode=0o700, exist_ok=True)
    with open(orch_state.path_of(".lock"), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def mine_now() -> int:
    with state_lock():
        scans = orch_state.load_cache().get("scans", {})
        targets = {local_id: scan.get("path", "") for local_id, scan in scans.items() if isinstance(scan, dict)}
        if not targets:
            raise MineError("no orchestrator transcript is known yet. Run: python3 scripts/orch_state.py build")
        newest = mine(targets)
    print(f"Mined {len(targets)} transcript(s). Newest decision: {newest[-1]['words'][:80] if newest else 'none'}.")
    return 0


def print_decisions(last: int) -> int:
    for item in read_log(DECISIONS_FILE)[-last:]:
        asked = f" (to: {item['question']})" if item.get("question") else ""
        print(f"{orch_state.clock(item.get('at', 0))} {item['kind']} [{item['channel']}]: {item['words'][:200]}{asked}")
    return 0


def print_candidates() -> int:
    for line in import_lines(read_log(CANDIDATES_FILE), ""):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Decision and correction miners")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mine")
    shown = sub.add_parser("decisions")
    shown.add_argument("--last", type=int, default=20)
    sub.add_parser("candidates")
    importer = sub.add_parser("import-rules")
    importer.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "mine":
            return mine_now()
        if args.command == "decisions":
            return print_decisions(args.last)
        if args.command == "candidates":
            return print_candidates()
        if not args.dry_run:
            print("import-rules writes nothing in this version. Run it with --dry-run to see the lines.", file=sys.stderr)
            return 2
        return dry_run()
    except (MineError, orch_state.StateError) as error:
        print(f"MINE FAILED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

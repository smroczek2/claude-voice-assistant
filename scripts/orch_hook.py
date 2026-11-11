"""Put the orchestrator state in front of the model.

  orch_hook.py start   SessionStart hook (startup, resume, compact)
  orch_hook.py turn    UserPromptSubmit hook, and the voice turn of the plugin
  orch_hook.py stop    Stop hook: makes a session in `card_nag` name a waiting card, at most once in 20 minutes

The hook reads the hook JSON on stdin and uses its `session_id`
(`--session` overrides it). It acts only for a session listed in
`orchestrators.json`. Every other session gets no output.

For an orchestrator it always prints one STATE line: `STATE as of HH:MM`, or
`STATE BUILD FAILED: <reason>` with the time of the last good build. It never
prints nothing and always exits 0, so a fault never blocks a turn.

A saved state younger than 2 minutes is served as it is. An older one starts a
rebuild and waits for it up to the limit (1.5 s for a turn). On a time-out it
serves the saved state with its age and the rebuild goes on in the background.
A turn prints the full digest only when its content changed since the last print
for this session; else it prints one line with the time, and the cards that
still wait for a click. The start print adds `rules.md`, with the list of
orchestrators put in place of `{{ORCHESTRATORS}}`.

Runs on the macOS system Python 3.9 with the standard library only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orch_state  # noqa: E402

VERSION = f"orch_hook v{orch_state.VERSION}"
REBUILD_AGE_S = 120
LIMITS = {"start": 8.0, "turn": 1.5}
BUILDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orch_state.py")


def read_payload() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def is_orchestrator(session_id: str) -> bool:
    """Cheap checks first: the listed ids, then the transcript ids that the last build mapped."""
    if not session_id or not os.path.exists(orch_state.path_of("orchestrators.json")):
        return False
    ids, _ = orch_state.load_orchestrators()
    if session_id in ids:
        return True
    mapping = (orch_state.load_state() or {}).get("cli_to_local", {})
    if mapping.get(session_id) in ids or any(mapping.get(i) == session_id for i in ids):
        return True
    unmapped = [i for i in ids if i.startswith("local_") and i not in mapping.values()]
    return bool(unmapped) and session_id in transcript_ids(unmapped)


def transcript_ids(local_ids: list[str]) -> set[str]:
    """Transcript ids of listed sessions that no build has mapped yet. Reads only their own records."""
    found = set()
    for local_id in local_ids:
        for path in orch_state.session_index.record_paths(f"{local_id}.json"):
            try:
                meta = orch_state.read_json(path, {})
            except (OSError, json.JSONDecodeError):
                continue
            found.update(i for i in [meta.get("cliSessionId"), *(meta.get("priorCliSessionIds") or [])] if i)
    return found


def named_in_file(session_id: str) -> bool:
    """For a broken orchestrators.json: does the raw text name this session?"""
    try:
        with open(orch_state.path_of("orchestrators.json"), encoding="utf-8") as handle:
            return bool(session_id) and session_id in handle.read()
    except OSError:
        return False


def last_good() -> str:
    state = orch_state.load_state()
    return orch_state.clock(state["built_at"], with_day=False) if state else "never"


def failed(reason: str) -> str:
    return f"STATE BUILD FAILED: {reason}; last good {last_good()}"


def fresh_state(limit: float) -> tuple[dict | None, str]:
    """The state to serve, and a note. A failure note starts with FAILED."""
    state = orch_state.load_state()
    if state and orch_state.now_s() - state["built_at"] < REBUILD_AGE_S:
        return state, ""
    process = subprocess.Popen([sys.executable, BUILDER, "build"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, start_new_session=True, env=os.environ.copy())
    try:
        _, err = process.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        if state:
            return state, f"rebuild still running after {limit:g} s; facts from {orch_state.clock(state['built_at'])}"
        return None, f"FAILED first build still running after {limit:g} s"
    if process.returncode != 0:
        reason = (err.decode("utf-8", errors="replace").strip().splitlines() or ["the build exited with an error"])[-1]
        return state, "FAILED " + reason.replace("STATE BUILD FAILED: ", "")
    return orch_state.load_state(), ""


def remember_print(session_id: str, digest: str) -> bool:
    """Record the digest hash for this session. True when it differs from the last print."""
    os.makedirs(orch_state.state_dir(), mode=0o700, exist_ok=True)
    with open(orch_state.path_of(".printed.lock"), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                printed = orch_state.read_json(orch_state.path_of("printed.json"), {})
            except json.JSONDecodeError:
                printed = {}
            changed = printed.get(session_id) != digest
            if changed:
                printed[session_id] = digest
                orch_state.write_atomic(orch_state.path_of("printed.json"), json.dumps(printed))
            return changed
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def local_id_of(state: dict, session_id: str) -> str:
    return state.get("cli_to_local", {}).get(session_id, session_id)


RULES_SHARE = 0.25  # at most this part of the start budget goes to rules.md
ROSTER_MARK = "{{ORCHESTRATORS}}"


def roster_text(state: dict, session_id: str) -> str:
    """One line per orchestrator: its link, its folder, and "you" for the session that reads it."""
    me = local_id_of(state, session_id)
    lines = [f"- [{item['title'] or item['id']}](#{item['id']}) folder {item['cwd'] or 'unknown'}"
             + (" (you)" if item["id"] == me else "") for item in state.get("roster", [])]
    return "\n".join(lines) or "- none found in the session records"


def read_rules(state: dict, session_id: str) -> str:
    path = orch_state.path_of("rules.md")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip().replace(ROSTER_MARK, roster_text(state, session_id))


def card_lines(state: dict, budget: int) -> str:
    """The waiting cards, cut to the budget, with a "+N more" line for the rest."""
    rows = [f"- {f['text']} [{f['source']}]" for f in state.get("facts", []) if f["kind"] == "card"]
    if not rows:
        return ""
    lines, used = [orch_state.HEADINGS["card"]], orch_state.tokens(orch_state.HEADINGS["card"]) + 12
    for row in rows:
        if used + orch_state.tokens(row) + 1 > budget:
            break
        lines.append(row)
        used += orch_state.tokens(row) + 1
    if len(lines) - 1 < len(rows):
        lines.append(f"+{len(rows) - len(lines) + 1} more cards (python3 scripts/orch_state.py show)")
    return "\n".join(lines)


def start_text(state: dict, session_id: str, note: str) -> str:
    """Digest, last turns and rules, all inside the start budget. The turns are kept whole."""
    budget = orch_state.BUDGETS["start"]
    turns = state.get("last_turns", {}).get(local_id_of(state, session_id), [])
    lines = ["LAST TURNS (each with what followed it)"]
    lines += [f"- {orch_state.clock(t['at'])} \"{t['text']}\": {t['label']}" for t in turns]
    turn_block = "\n".join(lines) if turns else ""
    rules = read_rules(state, session_id)
    rules_room = min(orch_state.tokens(rules) + 10, int(budget * RULES_SHARE)) if rules else 0
    digest_room = budget - orch_state.tokens(turn_block) - rules_room - 4
    parts = [orch_state.render(state, "start", orch_state.now_s(), note, digest_room), turn_block]
    if rules:
        cut = rules[: (rules_room - 10) * 4]
        parts.append("RULES\n" + cut + ("\n+ more rules in rules.md" if len(cut) < len(rules) else ""))
    remember_print(session_id, orch_state.digest_hash(state))
    return "\n\n".join(part for part in parts if part)


def turn_text(state: dict, session_id: str, note: str) -> str:
    """The full digest when it changed. Else the time, and the waiting cards again until they are clicked."""
    if remember_print(session_id, orch_state.digest_hash(state)):
        return orch_state.render(state, "turn", orch_state.now_s(), note)
    stamp = orch_state.stamp(state, orch_state.now_s(), note or "no change")
    cards = card_lines(state, orch_state.BUDGETS["turn"] - orch_state.HEAD_RESERVE)
    return stamp + ("\n" + cards if cards else "")


def output(mode: str, session_id: str, limit: float | None = None) -> str:
    """The text to print for one hook call. Empty only for a session that is not an orchestrator."""
    try:
        if not is_orchestrator(session_id):
            return ""
    except orch_state.StateError as error:
        return failed(str(error)) if named_in_file(session_id) else ""
    try:
        state, note = fresh_state(LIMITS[mode] if limit is None else limit)
        if note.startswith("FAILED "):
            return failed(note[len("FAILED "):])
        if not state:
            return failed("no state was written")
        return start_text(state, session_id, note) if mode == "start" else turn_text(state, session_id, note)
    except Exception as error:  # the hook must never block a turn; it reports the fault in the STATE line
        return failed(f"{type(error).__name__}: {error}")


# ---------- stop: the card nag ----------

NAG_SHOWN = 5  # cards named in one reminder; the rest are counted
REMIND_S = 20 * 60  # a card that still waits is named again after this time, not in every reply
TAIL_BYTES = 512 * 1024


def nag_sessions(state: dict) -> set[str]:
    """The local ids in `card_nag` of orchestrators.json. Empty when the key is missing."""
    try:
        data = orch_state.read_json(orch_state.path_of("orchestrators.json"), {})
    except (OSError, json.JSONDecodeError):
        return set()
    ids = data.get("card_nag") if isinstance(data, dict) else None
    return {str(i) for i in ids} if isinstance(ids, list) else set()


def last_reply(payload: dict) -> str:
    """The assistant text of this turn: the payload field when the app sends it, else the transcript tail."""
    text = payload.get("last_assistant_message")
    if isinstance(text, str):
        return text
    path = payload.get("transcript_path")
    if not path or not os.path.exists(path):
        return ""
    with open(path, "rb") as handle:
        handle.seek(max(0, os.path.getsize(path) - TAIL_BYTES))
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    parts = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (entry.get("message") or {}).get("content")
        if entry.get("type") == "assistant" and isinstance(content, list):
            parts += [b.get("text", "") for b in reversed(content) if isinstance(b, dict) and b.get("type") == "text"]
        elif entry.get("type") == "user" and not entry.get("isMeta") and is_prompt(content):
            break
    return "\n".join(reversed(parts))


def is_prompt(content) -> bool:
    """A real user prompt, not a tool result."""
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "text" for b in content)


def plain(text: str) -> str:
    for a, b in (("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"')):
        text = text.replace(a, b)
    return " ".join(text.lower().split())


def card_list(cards: list[dict]) -> str:
    shown = [f"'{c['title']}' in [{c['holder_title']}](#{c['holder']})" for c in cards[:NAG_SHOWN]]
    more = f"; and {len(cards) - NAG_SHOWN} more" if len(cards) > NAG_SHOWN else ""
    return "; ".join(shown) + more


def due_cards(local_id: str, cards: list[dict], reply: str, now: float) -> list[dict]:
    """The waiting cards that this session did not name in the last REMIND_S seconds.

    `nagged.json` holds, for each session, when each card was last named. A card named in this
    reply, or given back here as due, counts as named now. Cards that no longer wait are dropped."""
    os.makedirs(orch_state.state_dir(), mode=0o700, exist_ok=True)
    with open(orch_state.path_of(".printed.lock"), "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                nagged = orch_state.read_json(orch_state.path_of("nagged.json"), {})
            except json.JSONDecodeError:
                nagged = {}
            before = nagged.get(local_id) if isinstance(nagged.get(local_id), dict) else {}
            due = [c for c in cards if plain(c["title"]) not in reply and now - before.get(c["id"], 0) >= REMIND_S]
            after = {c["id"]: now if c in due or plain(c["title"]) in reply else before.get(c["id"], 0) for c in cards}
            if after != before:
                orch_state.write_atomic(orch_state.path_of("nagged.json"), json.dumps({**nagged, local_id: after}))
            return due
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def stop_decision(payload: dict) -> dict | None:
    """Block once when a card waits and this session did not name it in the last REMIND_S seconds.

    None means let the turn end. The user asked for reminders until he clicks (2026-11-10), but a fixed
    line at the end of every reply was read aloud each time (2026-11-11). So a card is due again only
    after REMIND_S, and the model says it in its own words."""
    if payload.get("stop_hook_active"):
        return None
    state = orch_state.load_state()
    local_id = local_id_of(state, str(payload.get("session_id", ""))) if state else ""
    if not state or local_id not in nag_sessions(state):
        return None
    now = orch_state.now_s()
    cards = [f["card"] for f in state.get("facts", []) if f["kind"] == "card" and isinstance(f.get("card"), dict)]
    cards = orch_state.still_waiting(cards, state["built_at"], now) if cards else []
    due = due_cards(local_id, cards, plain(last_reply(payload)), now)
    if not due:
        return None
    return {"decision": "block",
            "reason": "A CARD WAITS FOR THE USER'S CLICK, and you did not name it in the last "
                      f"{REMIND_S // 60} minutes: {card_list(due)}. Tell the user now in one short sentence, "
                      "in their words, and keep the session link. Write nothing else."}


def stop_main() -> int:
    """The Stop hook. Any fault lets the turn end: the fault goes to stderr, never into the reply."""
    try:
        decision = stop_decision(read_payload())
    except Exception as error:  # a broken nag must never hold a turn open
        print(f"orch_hook stop: {type(error).__name__}: {error}", file=sys.stderr)
        return 0
    if decision:
        print(json.dumps(decision))
    return 0


def main(argv: list[str] | None = None) -> int:
    if (argv if argv is not None else sys.argv[1:])[:1] == ["stop"]:
        return stop_main()
    parser = argparse.ArgumentParser(description="Orchestrator state hook")
    parser.add_argument("mode", choices=sorted(LIMITS))
    parser.add_argument("--session", default="")
    parser.add_argument("--limit", type=float, default=None)
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    session_id = args.session or str(read_payload().get("session_id", ""))
    started = time.monotonic()
    text = output(args.mode, session_id, args.limit)
    if text:
        print(text)
        if os.environ.get("VOZ_ORCH_TIMING"):
            print(f"(hook took {int((time.monotonic() - started) * 1000)} ms)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

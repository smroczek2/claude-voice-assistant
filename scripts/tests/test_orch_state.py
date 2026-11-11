"""The orchestrator state: built by code, shown at start and at each turn.

Every test uses temp folders and fake transcripts. No test reads or writes
~/.claude or the Desktop app's records.
"""

import ast
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import ledger  # noqa: E402
import orch_hook  # noqa: E402
import orch_state  # noqa: E402
import session_index  # noqa: E402

NOW = time.time()


def iso(seconds_ago: float) -> str:
    return dt.datetime.fromtimestamp(NOW - seconds_ago, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(text, ago):
    return {"type": "user", "timestamp": iso(ago), "message": {"role": "user", "content": text}}


def say(text, ago):
    return {"type": "assistant", "timestamp": iso(ago), "message": {"content": [{"type": "text", "text": text}]}}


def tool(tool_id, name, args, ago):
    return {"type": "assistant", "timestamp": iso(ago),
            "message": {"content": [{"type": "tool_use", "id": tool_id, "name": name, "input": args}]}}


def result(tool_id, text, ago):
    return {"type": "user", "timestamp": iso(ago),
            "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": [{"type": "text", "text": text}]}]}}


def compaction(ago):
    return [{"type": "system", "subtype": "compact_boundary", "timestamp": iso(ago), "content": "Conversation compacted"},
            {"type": "user", "isCompactSummary": True, "timestamp": iso(ago),
             "message": {"content": "This session is being continued from a previous conversation."}}]


class World:
    """Fake session records, transcripts and a state folder under one temp folder."""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.sessions = root / "sessions" / "acct" / "org"
        self.projects = root / "config" / "projects" / "-fake-project"
        self.orch = root / "orch"
        for folder in (self.sessions, self.projects):
            folder.mkdir(parents=True)
        monkeypatch.setenv("VOZ_ORCH_DIR", str(self.orch))
        monkeypatch.setenv("VOZ_DESKTOP_SESSIONS_DIR", str(root / "sessions"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root / "config"))
        monkeypatch.delenv("VOZ_VAULT_DIR", raising=False)
        monkeypatch.setattr(session_index, "SESSIONS_DIR", str(root / "sessions"))
        monkeypatch.setattr(session_index, "PROJECTS_DIR", str(root / "config" / "projects"))

    def session(self, local_id, cli_id, title, entries=(), **fields):
        record = {"sessionId": local_id, "cliSessionId": cli_id, "title": title,
                  "lastActivityAt": int((NOW - 60) * 1000), **fields}
        (self.sessions / f"{local_id}.json").write_text(json.dumps(record))
        with open(self.projects / f"{cli_id}.jsonl", "w") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def append(self, cli_id, *entries):
        with open(self.projects / f"{cli_id}.jsonl", "a") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def orchestrators(self, value):
        self.orch.mkdir(exist_ok=True)
        text = value if isinstance(value, str) else json.dumps(value)
        (self.orch / "orchestrators.json").write_text(text)

    def env(self):
        return {**os.environ, "VOZ_ORCH_DIR": str(self.orch), "VOZ_DESKTOP_SESSIONS_DIR": str(self.root / "sessions"),
                "CLAUDE_CONFIG_DIR": str(self.root / "config")}


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def facts_of(state, kind):
    return [f["text"] for f in state["facts"] if f["kind"] == kind]


# ---------- edge case 1: compaction in the middle of a relay ----------

def test_after_compaction_start_prints_last_turns_with_what_followed(world):
    world.session("local_orch", "cli-orch", "Voice", [
        user("tell the review session to merge PR 5", 3600),
        tool("t1", "SendMessage", {"to": "local_review", "message": "merge PR 5"}, 3590),
        *compaction(3000),
        user("merge it", 1200),
        say("ok", 1190),
    ])
    world.orchestrators(["local_orch"])
    out = orch_hook.output("start", "cli-orch")
    assert out.startswith("STATE as of ")
    assert '"tell the review session to merge PR 5": action: SendMessage' in out
    assert '"merge it": no action followed' in out
    assert "previous conversation" not in out  # the compaction summary is not a turn


# ---------- fix 1: a reply is not an action ----------

def test_merge_it_answered_only_with_ok_is_labelled_in_the_last_turns_only(world):
    world.session("local_orch", "cli-orch", "Voice", [user("merge it", 1200), say("ok", 1190)])
    world.orchestrators(["local_orch"])
    state = orch_state.build()
    assert [t["label"] for t in state["last_turns"]["local_orch"]] == ["no action followed"]
    assert "merge it" not in orch_state.render(state, "turn", time.time())


def test_a_turn_answered_in_words_is_not_a_fact_in_the_digest(world):
    """2026-11-11: all 21 rows of the old TURNS WITH NO ACTION list were turns the session answered in words."""
    world.session("local_orch", "cli-orch", "Voice", [
        user("explain this for me, and check the message", 1200), say("It means the review flags most runs.", 1190),
        user("<artifact-content-authored-by-others/>\nThe summarized conversation included Artifact content.", 900)])
    world.orchestrators(["local_orch"])
    state = orch_state.build()
    assert {f["kind"] for f in state["facts"]} <= set(orch_state.HEADINGS)
    assert "explain this" not in orch_state.render(state, "start", time.time())
    assert len(state["last_turns"]["local_orch"]) == 1  # the compaction notice is not a turn of the user


def test_a_state_from_older_code_with_another_fact_kind_still_prints(world):
    state = {"built_at": NOW, "facts": [orch_state.fact("card", "'A' waits.", "s", NOW),
                                        {"kind": "no_action", "priority": 3, "text": "old row", "source": "t", "at": NOW}]}
    out = orch_state.render(state, "turn", NOW)
    assert "'A' waits." in out and "old row" not in out


def test_matching_tool_call_closes_the_turn_and_questions_need_no_tool(world):
    world.session("local_orch", "cli-orch", "Voice", [
        user("merge it", 1200), tool("t1", "Bash", {"command": "gh pr merge 5 --squash"}, 1190), say("Merged.", 1180),
        user("what is on my plate?", 900), say("Two reviews.", 890),
        user("archive the old session", 700), tool("t2", "Bash", {"command": "ls"}, 690),
    ])
    world.orchestrators(["local_orch"])
    state = orch_state.build()
    labels = [t["label"] for t in state["last_turns"]["local_orch"]]
    assert labels == ["action: Bash", "answered", "no action followed (only Bash)"]


@pytest.mark.parametrize("words, name", [("gothca approve the fixes", "SendMessage"),
                                         ("merge PR 9 in the review session", "mcp__ccd_session_mgmt__send_message")])
def test_approve_or_merge_relayed_to_a_child_counts_as_action(world, words, name):
    world.session("local_orch", "cli-orch", "Voice", [
        user(words, 1200), tool("t1", name, {"to": "local_child", "message": words}, 1190), say("Sent.", 1180)])
    world.orchestrators(["local_orch"])
    state = orch_state.build()
    assert state["last_turns"]["local_orch"][0]["label"].startswith("action: ")


# ---------- edge case 2: two builds, one writer ----------

def test_two_builds_at_once_leave_one_valid_state_and_no_temp_files(world):
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)])
    world.orchestrators(["local_orch"])
    command = [sys.executable, str(SCRIPTS / "orch_state.py"), "build"]
    runs = [subprocess.Popen(command, env=world.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
    codes = [run.wait(timeout=30) for run in runs]
    assert codes == [0, 0, 0, 0], [run.stderr.read() for run in runs]
    state = json.loads((world.orch / "state.json").read_text())
    assert state["orchestrators"] == ["local_orch"]
    assert not [p.name for p in world.orch.iterdir() if p.name.startswith(".tmp-")]
    assert oct((world.orch / "state.json").stat().st_mode & 0o777) == "0o600"


# ---------- edge case 3: a failure is never silent ----------

def test_broken_orchestrators_file_prints_failed_line_for_a_named_session(world):
    world.orchestrators('["local_orch", ')
    out = orch_hook.output("turn", "local_orch")
    assert out.startswith("STATE BUILD FAILED: ") and "not valid JSON" in out and "last good never" in out
    assert orch_hook.output("turn", "local_other") == ""


def test_missing_records_folder_prints_failed_line(world):
    world.orchestrators(["local_orch"])
    shutil.rmtree(world.root / "sessions")
    out = orch_hook.output("turn", "local_orch")
    assert out.startswith("STATE BUILD FAILED: session records folder not found")


def test_hook_process_exits_zero_and_prints_failed_line(world):
    world.orchestrators("{broken")
    run = subprocess.run([sys.executable, str(SCRIPTS / "orch_hook.py"), "turn"], input=json.dumps(
        {"session_id": "broken"}), env=world.env(), capture_output=True, text=True, timeout=30)
    assert run.returncode == 0 and run.stdout.startswith("STATE BUILD FAILED: ")


def test_old_state_prints_as_stale():
    state = {"built_at": time.time() - 20 * 60, "facts": []}
    assert "STALE: 20 min old" in orch_state.stamp(state, time.time())


# ---------- edge case 8: too many facts for the budget ----------

def test_digest_is_cut_to_budget_by_priority_with_more_line():
    facts = [orch_state.fact("idle_pr", f"'Session {n}' idle since 11-0{n % 9 + 1} 10:00, open PR repo#{n}.",
                             f"session record local_{n}", NOW - n) for n in range(200)]
    facts.insert(0, orch_state.fact("alert", "Orchestrator 'Voice' is archived.", "session record local_orch", NOW))
    state = {"built_at": NOW, "facts": facts}
    for mode in ("turn", "start"):
        text = orch_state.render(state, mode, NOW)
        assert orch_state.tokens(text) <= orch_state.BUDGETS[mode]
        lines = text.splitlines()
        assert lines[1] == "ALERTS" and "archived" in lines[2]
        shown = sum(line.startswith("- ") for line in lines)
        assert lines[-1].startswith(f"+{201 - shown} more")


def test_start_output_with_turns_and_rules_stays_in_budget(world):
    entries = []
    for n in range(40):
        entries += [user(f"please check item {n} " + "x" * 300, 5000 - n * 60), say("done", 4990 - n * 60)]
    world.session("local_orch", "cli-orch", "Voice", entries)
    world.orchestrators(["local_orch"])
    (world.orch / "rules.md").write_text("\n".join(f"- 2026-11-10 rule {n}: " + "y" * 200 for n in range(200)))
    out = orch_hook.output("start", "local_orch")
    assert orch_state.tokens(out) <= orch_state.BUDGETS["start"] + 20
    last_turns = out.split("LAST TURNS", 1)[1].split("RULES", 1)[0]
    assert last_turns.count('"please check item') == orch_state.LAST_TURNS
    assert "+ more rules in rules.md" in out


# ---------- edge case 11: the hooks are global ----------

def test_session_not_listed_gets_nothing(world):
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)])
    assert orch_hook.output("turn", "cli-orch") == ""  # no orchestrators.json at all
    world.orchestrators(["local_orch"])
    assert orch_hook.output("turn", "cli-other") == ""
    assert orch_hook.output("start", "") == ""
    assert orch_hook.output("turn", "cli-orch").startswith("STATE as of ")  # mapped from its own record


def test_one_or_more_ids_and_object_form(world):
    world.session("local_a", "cli-a", "Voice", [user("hello", 100)])
    world.session("local_b", "cli-b", "Vault", [user("hello", 100)])
    world.orchestrators({"sessions": ["local_a", "cli-b"]})
    state = orch_state.build()
    assert state["orchestrators"] == ["local_a", "local_b"]
    assert orch_hook.output("turn", "local_b").startswith("STATE as of ")


# ---------- edge case 12: one line when nothing changed ----------

def test_turn_prints_full_digest_only_when_it_changed(world):
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)])
    world.session("local_child", "cli-child", "Review", [
        tool("q1", "AskUserQuestion", {"questions": [{"question": "Merge PR 7?", "options": [{"label": "Yes"}]}]}, 50)])
    world.orchestrators(["local_orch"])
    first = orch_hook.output("turn", "local_orch")
    assert "QUESTIONS OPEN IN OTHER SESSIONS" in first and "Merge PR 7?" in first
    second = orch_hook.output("turn", "local_orch")
    assert second.count("\n") == 0 and second.endswith("(no change)")
    world.append("cli-child", result("q1", "Yes", 10), say("Merged.", 5))
    os.utime(world.projects / "cli-child.jsonl", (time.time() + 5, time.time() + 5))
    (world.orch / "state.json").unlink()
    third = orch_hook.output("turn", "local_orch")
    assert "Merge PR 7?" not in third and "\n" in third


# ---------- fix 3: the 1.5-second limit ----------

def slow_builder(tmp_path, monkeypatch):
    script = tmp_path / "slow_build.py"
    script.write_text("import time\ntime.sleep(5)\n")
    monkeypatch.setattr(orch_hook, "BUILDER", str(script))


def test_slow_rebuild_serves_saved_state_with_its_age(world, tmp_path, monkeypatch):
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)])
    world.orchestrators(["local_orch"])
    orch_state.build()
    state = json.loads((world.orch / "state.json").read_text())
    state["built_at"] -= 600
    (world.orch / "state.json").write_text(json.dumps(state))
    slow_builder(tmp_path, monkeypatch)
    started = time.monotonic()
    out = orch_hook.output("turn", "local_orch")
    assert time.monotonic() - started < 2.5
    assert out.startswith("STATE as of ") and "rebuild still running after 1.5 s" in out


def test_slow_first_build_prints_failed_line(world, tmp_path, monkeypatch):
    world.orchestrators(["local_orch"])
    slow_builder(tmp_path, monkeypatch)
    out = orch_hook.output("turn", "local_orch")
    assert out.startswith("STATE BUILD FAILED: first build still running after 1.5 s")


# ---------- the facts ----------

def card(task_id, title, ago, life=7 * 86400):
    made = int((NOW - ago) * 1000)
    return {"id": task_id, "title": title, "prompt": "brief", "createdAt": made, "expiresAt": made + life * 1000}


def test_waiting_cards_come_from_records_with_age_and_holder_link(world):
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)])
    world.session("local_plans", "cli-plans", "Plans", [], backgroundTaskSuggestions=[
        card("task_open", "Open card", 3 * 3600), card("task_gone", "Expired", 10, life=5),
        card("task_done", "Started", 600)], resolvedBackgroundTaskSuggestions={"task_done": "started_notified"})
    world.session("local_child", "cli-child", "Child", [], backgroundTaskSuggestions=[card("task_kid", "Kid card", 120)])
    world.session("local_old", "cli-old", "Archived", [], isArchived=True,
                  backgroundTaskSuggestions=[card("task_arch", "Archived card", 60)])
    world.orchestrators(["local_orch", "local_plans"])
    cards = facts_of(orch_state.build(), "card")
    assert cards == ["'Kid card' waiting 2 min, held by [Child](#local_child) (task_kid).",
                     "'Open card' waiting 3 h, held by [Plans](#local_plans) (task_open)."]


def test_turn_with_no_change_repeats_the_waiting_cards(world):
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)],
                  backgroundTaskSuggestions=[card("task_a", "Card A", 300)])
    world.orchestrators(["local_orch"])
    first = orch_hook.output("turn", "local_orch")
    second = orch_hook.output("turn", "local_orch")
    assert "CARDS WAITING FOR A CLICK" in first
    assert "(no change)" in second and "'Card A' waiting 5 min, held by [Voice](#local_orch)" in second
    assert orch_state.tokens(second) <= orch_state.BUDGETS["turn"]


def test_turn_with_no_change_and_no_cards_is_one_line(world):
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)])
    world.orchestrators(["local_orch"])
    orch_hook.output("turn", "local_orch")
    assert len(orch_hook.output("turn", "local_orch").splitlines()) == 1


def test_repeated_cards_stay_in_the_turn_budget(world):
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)],
                  backgroundTaskSuggestions=[card(f"task_{n}", "card " + "z" * 120, 300 + n) for n in range(60)])
    world.orchestrators(["local_orch"])
    orch_hook.output("turn", "local_orch")
    second = orch_hook.output("turn", "local_orch")
    assert orch_state.tokens(second) <= orch_state.BUDGETS["turn"]
    assert second.splitlines()[-1].startswith("+") and "more cards" in second


def test_start_prints_rules_with_the_live_orchestrator_list(world):
    world.session("local_voice", "cli-voice", "Voice", [user("hello", 100)], cwd="/p/voice")
    world.session("local_plans", "cli-plans", "Plans", [], cwd="/p/plans")
    world.orchestrators(["local_voice", "local_plans"])
    (world.orch / "rules.md").write_text("ORCHESTRATORS\n{{ORCHESTRATORS}}\nRULE one")
    out = orch_hook.output("start", "cli-voice")
    assert "- [Voice](#local_voice) folder /p/voice (you)" in out
    assert "- [Plans](#local_plans) folder /p/plans\n" in out
    assert "{{ORCHESTRATORS}}" not in out and "RULE one" in out


def test_shipped_rules_fit_the_rules_share_of_the_start_budget():
    text = Path(orch_state.RULES_SOURCE).read_text()
    assert "{{ORCHESTRATORS}}" in text and "the SAME title" in text
    assert orch_state.tokens(text) + 200 < orch_state.BUDGETS["start"] * orch_hook.RULES_SHARE


def test_install_rules_copies_and_keeps_a_changed_copy(world, capsys):
    assert orch_state.main(["install-rules"]) == 0
    target = world.orch / "rules.md"
    assert target.read_text() == Path(orch_state.RULES_SOURCE).read_text()
    assert orch_state.main(["install-rules"]) == 0 and "is current" in capsys.readouterr().out
    target.write_text("my edit")
    assert orch_state.main(["install-rules"]) == 1 and target.read_text() == "my edit"
    assert orch_state.main(["install-rules", "--force"]) == 0
    assert (world.orch / "rules.md.bak").read_text() == "my edit"
    assert target.read_text() == Path(orch_state.RULES_SOURCE).read_text()


# ---------- the cache across code versions ----------

# The last commit before the state code changed its cache and state files (PR 8). These tests
# check that this version and that older version can share one state folder. A fixed commit, not a
# moving ref such as origin/main: when main moves on, the "old" code must stay the same.
V1_COMMIT = "9fa646d877c430c30533bec98506d2402ce1cd78"


def main_code(tmp_path):
    """The scripts at V1_COMMIT, the older code that may still run beside this version."""
    folder = tmp_path / "main_scripts"
    folder.mkdir()
    for name in ("orch_state.py", "orch_hook.py", "ledger.py", "session_index.py"):
        shown = subprocess.run(["git", "-C", str(SCRIPTS), "show", f"{V1_COMMIT}:scripts/{name}"],
                               capture_output=True, text=True)
        if shown.returncode != 0:
            pytest.skip(f"commit {V1_COMMIT} is not in this clone")
        (folder / name).write_text(shown.stdout)
    return folder


def cards_world(world):
    world.session("local_orch", "cli-orch", "Voice", [user("merge it", 1200), say("ok", 1190)],
                  backgroundTaskSuggestions=[card("task_a", "Card A", 300)], cwd="/p/voice")
    world.orchestrators(["local_orch"])


def test_a_cache_from_main_is_not_read_and_does_not_break_the_build(world):
    cards_world(world)
    path = next(world.sessions.glob("local_orch.json"))
    old_meta = {k: v for k, v in orch_state.slim(json.loads(path.read_text())).items() if k not in ("cwd", "cards")}
    old_meta["resolvedCards"] = []
    stale = {"records": {str(path): {"mtime": path.stat().st_mtime, "meta": old_meta}}, "scans": {"local_orch": {"cards": {}}}}
    (world.orch / "cache.json").write_text(json.dumps(stale))
    (world.orch / orch_state.CACHE_FILE).write_text(json.dumps({**stale, "schema": orch_state.CACHE_SCHEMA}))
    state = orch_state.build()
    assert state["roster"] == [{"id": "local_orch", "title": "Voice", "cwd": "/p/voice"}]
    assert any("'Card A'" in text for text in facts_of(state, "card"))
    assert json.loads((world.orch / "cache.json").read_text()) == stale  # main's own file is left alone


def test_main_hook_reads_this_branch_state_as_last_good_with_no_main_build(world, tmp_path):
    cards_world(world)
    orch_state.build()
    old = main_code(tmp_path)
    for _ in range(2):
        # A limit near zero stands for a cold build of real records (about 3 s), longer than the 1.5 s limit.
        hook = subprocess.run([sys.executable, str(old / "orch_hook.py"), "turn", "--session", "local_orch",
                               "--limit", "0.001"], env=world.env(), capture_output=True, text=True)
        assert hook.stdout.startswith("STATE as of ") and "FAILED" not in hook.stdout, hook.stdout


def test_a_state_from_main_is_built_again_to_get_the_roster(world, tmp_path):
    cards_world(world)
    old = main_code(tmp_path)
    built = subprocess.run([sys.executable, str(old / "orch_state.py"), "build"], env=world.env(), capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    assert orch_state.load_state() is None
    (world.orch / "rules.md").write_text("{{ORCHESTRATORS}}")
    assert "- [Voice](#local_orch) folder /p/voice (you)" in orch_hook.output("start", "local_orch")


def test_a_cache_with_another_schema_is_dropped(world):
    cards_world(world)
    (world.orch / orch_state.CACHE_FILE).write_text(json.dumps({"schema": 1, "records": "broken"}))
    assert facts_of(orch_state.build(), "card")
    assert json.loads((world.orch / orch_state.CACHE_FILE).read_text())["schema"] == orch_state.CACHE_SCHEMA


def test_main_code_still_works_after_this_branch_wrote_the_state_folder(world, tmp_path):
    cards_world(world)
    orch_state.build()
    orch_hook.output("turn", "local_orch")
    old = main_code(tmp_path)
    env = world.env()
    built = subprocess.run([sys.executable, str(old / "orch_state.py"), "build"], env=env, capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    hook = subprocess.run([sys.executable, str(old / "orch_hook.py"), "turn", "--session", "local_orch"],
                          env=env, capture_output=True, text=True)
    assert hook.stdout.startswith("STATE as of ") and "FAILED" not in hook.stdout
    assert orch_state.build()["version"] == orch_state.VERSION  # and this branch reads its own cache again


def test_orchestrator_alerts_and_idle_sessions_with_open_prs(world):
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)], isArchived=True,
                  prs=[{"prNumber": 2, "repo": "org/voice", "state": "MERGED"}])
    world.session("local_idle", "cli-idle", "Idle work", [], lastActivityAt=int((NOW - 2 * 86400) * 1000),
                  prs=[{"prNumber": 9, "repo": "org/app", "state": "OPEN"}])
    world.session("local_busy", "cli-busy", "Busy work", [], prs=[{"prNumber": 10, "repo": "org/app", "state": "OPEN"}])
    world.orchestrators(["local_orch", "local_gone"])
    state = orch_state.build()
    alerts = facts_of(state, "alert")
    assert any("archived" in a for a in alerts)
    assert not any("org/voice#2" in a for a in alerts)
    assert any("local_gone is not in the session records" in a for a in alerts)
    idle = facts_of(state, "idle_pr")
    assert len(idle) == 1 and "org/app#9" in idle[0]
    assert all(f["source"] and f["at"] for f in state["facts"])


def test_work_idle_for_months_is_not_listed_as_stalled(world):
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)])
    world.session("local_old", "cli-old", "Summer work", [], lastActivityAt=int((NOW - 100 * 86400) * 1000),
                  prs=[{"prNumber": 3, "repo": "org/app", "state": "OPEN"}])
    world.orchestrators(["local_orch"])
    assert facts_of(orch_state.build(), "idle_pr") == []


def test_open_bound_pr_alerts_and_closed_bound_prs_do_not(world):
    closed = [{"prNumber": n, "repo": "org/plans", "state": "MERGED" if n % 2 else "CLOSED"} for n in range(1, 17)]
    world.session("local_orch", "cli-orch", "Plans work", [user("hello", 100)],
                  prs=closed + [{"prNumber": 128, "repo": "org/usage", "state": "OPEN"}])
    world.orchestrators(["local_orch"])
    alerts = facts_of(orch_state.build(), "alert")
    assert len(alerts) == 1
    assert "open PR org/usage#128 bound" in alerts[0] and "can auto-archive the orchestrator" in alerts[0]


def test_only_closed_bound_prs_give_no_alert(world):
    closed = [{"prNumber": n, "repo": "org/plans", "state": "MERGED" if n % 2 else "CLOSED"} for n in range(1, 17)]
    world.session("local_orch", "cli-orch", "Plans work", [user("hello", 100)], prs=closed)
    world.orchestrators(["local_orch"])
    assert facts_of(orch_state.build(), "alert") == []


def test_bound_pr_with_no_state_alerts_as_unknown(world):
    world.session("local_orch", "cli-orch", "Plans work", [user("hello", 100)],
                  prs=[{"prNumber": 7, "repo": "org/plans"}, {"prNumber": 8, "repo": "org/plans", "state": "CLOSED"}])
    world.orchestrators(["local_orch"])
    alerts = facts_of(orch_state.build(), "alert")
    assert alerts == ["Orchestrator 'Plans work' has PR org/plans#7 bound (state unknown). "
                      "Closing it can auto-archive the orchestrator."]


def test_archived_orchestrator_with_closed_prs_still_alerts(world):
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)], isArchived=True,
                  prs=[{"prNumber": 2, "repo": "org/voice", "state": "CLOSED"}])
    world.orchestrators(["local_orch"])
    alerts = facts_of(orch_state.build(), "alert")
    assert alerts == ["Orchestrator 'Voice' is archived."]


def test_vault_date_and_stale_vault(world, monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    note = vault / "Meetings" / "note.md"
    note.parent.mkdir()
    note.write_text("x")
    old = NOW - 10 * 86400
    os.utime(note, (old, old))
    (vault / ".obsidian" / "fresh.md").write_text("ignored")
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)])
    world.orchestrators({"sessions": ["local_orch"], "vault_dir": str(vault)})
    stale = facts_of(orch_state.build(), "vault")
    assert stale and stale[0].startswith("Vault stale since") and "live connectors" in stale[0]
    os.utime(note, (NOW, NOW))
    (world.orch / "state.json").unlink()
    assert facts_of(orch_state.build(), "vault")[0].startswith("Vault newest note")


def test_build_reads_only_new_transcript_lines(world):
    world.session("local_orch", "cli-orch", "Voice", [user("first", 300)])
    world.orchestrators(["local_orch"])
    orch_state.build()
    world.append("cli-orch", user("second", 100))
    (world.orch / "state.json").unlink()
    state = orch_state.build()
    assert [t["text"] for t in state["last_turns"]["local_orch"]] == ["first", "second"]
    cache = json.loads((world.orch / orch_state.CACHE_FILE).read_text())
    assert cache["scans"]["local_orch"]["offset"] == (world.projects / "cli-orch.jsonl").stat().st_size


def test_injected_state_and_voice_facts_are_not_user_turns(world):
    world.session("local_orch", "cli-orch", "Voice", [
        user('{"source":"voz-runtime","voiceState":"listening"}\nSTATE as of 10:42', 300), user("real words", 200)])
    world.orchestrators(["local_orch"])
    assert [t["text"] for t in orch_state.build()["last_turns"]["local_orch"]] == ["real words"]


# ---------- the Orch store removal ----------

def test_ledger_no_longer_reads_the_orch_store():
    source = (SCRIPTS / "ledger.py").read_text()
    assert "orch_inline" not in source and not hasattr(ledger, "ORCH_STORE") and not hasattr(ledger, "orch_seed")


def test_saved_descriptions_survive_without_the_orch_store():
    old = {"what": "Voice engine split", "why": "Share the engine", "nickname": "engine split",
           "started_with": "Brief the Orch mod wrote", "standing": "Waiting on review", "aliases": []}
    meta = {"sessionId": "local_old", "title": "Engine split", "lastActivityAt": 1}
    facts = {"first": {"text": "first prompt"}, "last_user": None, "last_relay": None, "question": None, "closing": ""}
    entry = ledger.build_entry(meta, facts, old, set())
    assert (entry["what"], entry["why"], entry["started_with"], entry["standing"]) == (
        "Voice engine split", "Share the engine", "Brief the Orch mod wrote", "Waiting on review")
    fresh = ledger.build_entry(meta, facts, {}, set())
    assert fresh["started_with"] == "first prompt" and fresh["what"] == ""


# ---------- Python 3.9, standard library only ----------

ALLOWED = {"__future__", "argparse", "datetime", "fcntl", "glob", "hashlib", "json", "math", "os", "re", "subprocess",
           "sys", "tempfile", "time", "ledger", "session_index", "orch_state", "orch_mine", "orch_guards", "difflib", "dataclasses"}


@pytest.mark.parametrize("name", ["orch_state.py", "orch_hook.py", "orch_guards.py"])
def test_new_scripts_use_only_the_standard_library(name):
    tree = ast.parse((SCRIPTS / name).read_text())
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported <= ALLOWED, imported - ALLOWED


@pytest.mark.skipif(not os.path.exists("/usr/bin/python3"), reason="no system Python on this machine")
def test_new_scripts_run_on_system_python(world):
    world.orchestrators(["local_orch"])
    world.session("local_orch", "cli-orch", "Voice", [user("hello", 100)])
    run = subprocess.run(["/usr/bin/python3", str(SCRIPTS / "orch_hook.py"), "start", "--session", "local_orch"],
                         env=world.env(), capture_output=True, text=True, timeout=60)
    assert run.returncode == 0 and run.stdout.startswith("STATE as of "), run.stderr
    version = subprocess.run(["/usr/bin/python3", str(SCRIPTS / "orch_hook.py"), "--version", "start"],
                             capture_output=True, text=True, timeout=60)
    assert version.stdout.strip() == orch_hook.VERSION


# ---------- voice turns: the command the plugin runs ----------

def run_plugin_command(world, scripts: Path, session_id: str) -> subprocess.CompletedProcess:
    """The exact argv that hooks/register.tsx (orchestratorState) runs for a voice turn."""
    argv = ["/usr/bin/python3", str(scripts / "ledger.py"), "state", "--session", session_id,
            "--budget", "turn", "--limit", "1.5"]
    return subprocess.run(argv, env=world.env(), capture_output=True, text=True, timeout=10)


@pytest.fixture
def engine_with_space(tmp_path):
    """A copy of the engine scripts in a folder whose path has spaces, as a plugin folder may."""
    target = tmp_path / "Application Support" / "voice engine" / "scripts"
    shutil.copytree(SCRIPTS, target, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    return target


def test_voice_turn_in_an_orchestrator_prints_the_digest(world, engine_with_space):
    world.session("local_orch", "cli-orch", "Voice", [user("merge it", 1200), say("ok", 1190)])
    world.session("local_plans", "cli-plans", "Plans", [], backgroundTaskSuggestions=[card("task_a", "Fix the gate", 3600)])
    world.orchestrators(["local_orch"])
    started = time.monotonic()
    done = run_plugin_command(world, engine_with_space, "local_orch")
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("STATE as of ")
    assert "CARDS WAITING FOR A CLICK" in done.stdout
    assert time.monotonic() - started < 2.5  # the plugin kills the command at 2.5 s


def test_voice_turn_in_another_session_prints_nothing(world, engine_with_space):
    world.session("local_orch", "cli-orch", "Voice", [user("merge it", 1200)])
    world.session("local_work", "cli-work", "Work", [user("fix the bug", 600)])
    world.orchestrators(["local_orch"])
    for session_id in ("local_work", "cli-work", "unknown-id"):
        done = run_plugin_command(world, engine_with_space, session_id)
        assert (done.returncode, done.stdout) == (0, ""), done.stderr


def test_voice_turn_with_no_orchestrator_list_prints_nothing(world, engine_with_space):
    world.session("local_orch", "cli-orch", "Voice", [user("merge it", 1200)])
    done = run_plugin_command(world, engine_with_space, "local_orch")
    assert (done.returncode, done.stdout) == (0, ""), done.stderr


# ---------- the card nag (Stop hook) ----------

def nag_world(world, **plans_fields):
    world.session("local_voice", "cli-voice", "Voice", [user("hello", 100), say("ok", 90)])
    world.session("local_plans", "cli-plans", "Plans", [], **{
        "backgroundTaskSuggestions": [card("task_a", "Fix the gate", 3600)], **plans_fields})
    world.orchestrators({"sessions": ["local_voice", "local_plans"], "card_nag": ["local_voice"]})
    orch_state.build()


def stop(world, reply, **extra):
    return orch_hook.stop_decision({"session_id": "cli-voice", "last_assistant_message": reply, **extra})


def test_stop_blocks_a_reply_that_does_not_name_the_waiting_card(world):
    nag_world(world)
    decision = stop(world, "Done. The tests pass.")
    assert decision["decision"] == "block"
    assert "'Fix the gate' in [Plans](#local_plans)" in decision["reason"]
    assert "word for word" not in decision["reason"] and " old" not in decision["reason"]


def test_stop_lets_a_reply_that_names_the_card_end(world):
    nag_world(world)
    assert stop(world, "Done. One card waits for you in Plans: ‘FIX the  gate’.") is None


def test_stop_names_a_card_again_only_after_twenty_minutes(world, monkeypatch):
    """2026-11-11: the old hook put the same line at the end of every reply, 13 times in 15 minutes."""
    nag_world(world)
    assert stop(world, "Done.")["decision"] == "block"
    assert stop(world, "Next answer.") is None
    assert stop(world, "Another answer.") is None
    later = orch_state.now_s() + orch_hook.REMIND_S + 1
    monkeypatch.setattr(orch_state, "now_s", lambda: later)
    assert stop(world, "Much later.")["decision"] == "block"


def test_a_card_named_in_a_reply_is_not_due_for_twenty_minutes(world):
    nag_world(world)
    assert stop(world, "A card waits in Plans: Fix the gate.") is None
    assert stop(world, "Something else.") is None


def test_a_new_card_is_due_at_once_while_an_old_one_rests(world):
    nag_world(world)
    assert stop(world, "Done.")["decision"] == "block"
    world.session("local_plans", "cli-plans", "Plans", [], backgroundTaskSuggestions=[
        card("task_a", "Fix the gate", 3600), card("task_b", "Write the notes", 60)])
    orch_state.build()
    reason = stop(world, "Done.")["reason"]
    assert "Write the notes" in reason and "Fix the gate" not in reason


def test_stop_never_blocks_its_own_retry(world):
    nag_world(world)
    assert stop(world, "Done.", stop_hook_active=True) is None


def test_stop_ignores_a_session_not_in_card_nag(world):
    nag_world(world)
    assert orch_hook.stop_decision({"session_id": "cli-plans", "last_assistant_message": "Done."}) is None


def test_stop_drops_a_card_clicked_after_the_state_was_built(world):
    nag_world(world)
    world.session("local_plans", "cli-plans", "Plans", [], backgroundTaskSuggestions=[card("task_a", "Fix the gate", 3600)],
                  resolvedBackgroundTaskSuggestions={"task_a": "started_notified"})
    assert stop(world, "Done.") is None


def test_stop_drops_a_card_whose_session_started_before_the_holder_recorded_it(world):
    nag_world(world)
    world.session("local_kid", "cli-kid", "Fix the gate", [], spawnedFrom={"sessionId": "local_plans", "taskId": "task_a"})
    assert stop(world, "Done.") is None


def test_a_click_held_only_in_the_other_account_copy_still_counts(world):
    world.session("local_voice", "cli-voice", "Voice", [user("hello", 100)])
    world.session("local_plans", "cli-plans", "Plans", [], backgroundTaskSuggestions=[card("task_a", "Fix the gate", 3600)])
    other = world.root / "sessions" / "acct2" / "org"
    other.mkdir(parents=True)
    old = {"sessionId": "local_plans", "cliSessionId": "cli-plans", "title": "Plans", "lastActivityAt": int((NOW - 9000) * 1000),
           "backgroundTaskSuggestions": [card("task_a", "Fix the gate", 3600)],
           "resolvedBackgroundTaskSuggestions": {"task_a": "dismissed"}}
    (other / "local_plans.json").write_text(json.dumps(old))
    world.orchestrators(["local_voice"])
    assert facts_of(orch_state.build(), "card") == []


def test_stop_reads_the_reply_from_the_transcript_when_the_payload_has_none(world, tmp_path):
    nag_world(world)
    path = tmp_path / "t.jsonl"
    entries = [user("old ask", 50), say("Waiting for your click: 'Fix the gate'.", 40), user("new ask", 30),
               say("Part one.", 20), result("t1", "tool output", 15), say("Part two.", 10)]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    decision = orch_hook.stop_decision({"session_id": "cli-voice", "transcript_path": str(path)})
    assert decision and decision["decision"] == "block"
    assert orch_hook.last_reply({"transcript_path": str(path)}) == "Part one.\nPart two."


def test_stop_with_a_broken_state_lets_the_turn_end(world, capsys, monkeypatch):
    nag_world(world)
    monkeypatch.setattr(orch_state, "load_state", lambda: (_ for _ in ()).throw(ValueError("bad")))
    monkeypatch.setattr(orch_hook, "read_payload", lambda: {"session_id": "cli-voice"})
    assert orch_hook.main(["stop"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and "ValueError: bad" in out.err


def test_card_list_names_five_cards_and_counts_the_rest():
    cards = [{"title": f"T{n}", "holder": "local_h", "holder_title": "H", "at": NOW - 60} for n in range(7)]
    line = orch_hook.card_list(cards)
    assert line.count("[H](#local_h)") == 5 and line.endswith("; and 2 more")


def test_a_card_only_in_the_copy_of_another_account_does_not_wait(world):
    """2026-11-11: after an account switch, the copy in the old account folder still listed two cards.
    The app showed neither, and the Stop hook named them in every reply."""
    world.session("local_voice", "cli-voice", "Voice", [user("hello", 100), say("ok", 90)])
    world.session("local_plans", "cli-plans", "Plans", [])
    old_account = world.root / "sessions" / "old-acct" / "org"
    old_account.mkdir(parents=True)
    stale = {"sessionId": "local_plans", "cliSessionId": "cli-plans", "title": "Plans", "lastActivityAt": int((NOW - 30) * 1000),
             "backgroundTaskSuggestions": [card("task_a", "Fix the gate", 3600)]}
    (old_account / "local_plans.json").write_text(json.dumps(stale))
    os.utime(old_account / "local_plans.json", (NOW - 3600, NOW - 3600))
    world.orchestrators({"sessions": ["local_voice"], "card_nag": ["local_voice"]})
    assert facts_of(orch_state.build(), "card") == []
    assert stop(world, "Done.") is None


def test_the_account_in_use_is_the_one_with_the_newest_record(world):
    other = world.root / "sessions" / "acct2" / "org"
    other.mkdir(parents=True)
    world.session("local_a", "cli-a", "A", [])
    (other / "local_b.json").write_text(json.dumps({"sessionId": "local_b"}))
    os.utime(other / "local_b.json", (NOW + 600, NOW + 600))
    assert [os.path.basename(p) for p in session_index.record_paths()] == ["local_b.json"]
    assert session_index.record_paths("local_a.json") == []

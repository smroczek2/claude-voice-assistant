"""Guards on orchestrator children, and test cases from real failures.

Every test uses temp folders (VOZ_ORCH_DIR and fake records). No test reads or
writes ~/.claude or the Desktop app's records.
"""

import datetime as dt
import json
import os
import subprocess
import sys

import pytest
from test_orch_state import (
    NOW,
    SCRIPTS,
    World,
    compaction,
    facts_of,
    iso,
    main_code,
    result,
    say,
    tool,
    user,
)

sys.path.insert(0, str(SCRIPTS))
import orch_guards  # noqa: E402
import orch_hook  # noqa: E402
import orch_state  # noqa: E402

REPO = "/p/repo"
VAULT = "/p/vault"
VOICE = "/p/voice"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def started(tool_id, args, child_id, ago):
    return [tool(tool_id, "mcp__ccd_session__start_session", args, ago),
            result(tool_id, f'Started session "{args.get("title", "")}" (session_id: {child_id}, name: x). '
                            "It runs on its own.", ago - 1)]


def proposed(tool_id, args, task_id, ago):
    return [tool(tool_id, "mcp__ccd_session__start_session", args, ago),
            result(tool_id, f'Proposed "{args.get("title", "")}" (task_id: {task_id}). The user sees a card.', ago - 1)]


def ran(model, cwd, ago):
    return {"type": "assistant", "timestamp": iso(ago), "cwd": cwd,
            "message": {"model": model, "content": [{"type": "text", "text": "working"}]}}


def child(world, local_id, model, cwd, ago=3600, transcript_model=None, transcript_cwd=None, **fields):
    entries = [ran(transcript_model or model, transcript_cwd or cwd, ago - 60)]
    spawned = {"sessionId": "local_orch", "taskId": fields.pop("task_id", "")}
    world.session(local_id, f"cli-{local_id}", fields.pop("title", f"Child {local_id}"), entries,
                  model=model, cwd=cwd, createdAt=int((NOW - ago) * 1000), spawnedFrom=spawned, **fields)


def orchestrator(world, entries, cwd=VOICE):
    world.session("local_orch", "cli-orch", "Voice", entries, cwd=cwd)
    world.orchestrators(["local_orch"])


def alerts(world):
    return facts_of(orch_state.build(), "alert")


BUILD = {"initiation": "user_asked", "title": "Build PR 3", "cwd": REPO, "use_worktree": True,
         "branch_name": "pr3", "prompt": "Build it."}


# ---------- model guard ----------

def test_build_child_on_fable_alerts_and_names_both_models(world):
    orchestrator(world, [user("start the build", 4000), *started("t1", BUILD, "local_c1", 3990)])
    child(world, "local_c1", "claude-fable-5-2", f"{REPO}/.claude/worktrees/pr3")
    found = alerts(world)
    assert len(found) == 1
    assert "[Child local_c1](#local_c1) runs claude-fable-5-2, but [Voice](#local_orch) asked for opus" in found[0]
    assert "build child with no model set" in found[0]


def test_build_child_on_opus_and_child_with_the_model_asked_give_no_alert(world):
    review = {"initiation": "user_asked", "title": "Review", "cwd": REPO, "model": "claude-haiku-5-5"}
    orchestrator(world, [*started("t1", BUILD, "local_c1", 3990), *started("t2", review, "local_c2", 3980)])
    child(world, "local_c1", "claude-opus-5-5", f"{REPO}/.claude/worktrees/pr3")
    child(world, "local_c2", "claude-haiku-5-5[1m]", REPO)
    assert alerts(world) == []


def test_explicit_model_wins_over_the_build_default_and_the_transcript_wins_over_the_record(world):
    asked = {**BUILD, "model": "claude-sonnet-5-5"}
    orchestrator(world, [*started("t1", asked, "local_c1", 3990)])
    child(world, "local_c1", "claude-sonnet-5-5", REPO, transcript_model="claude-fable-5-2")
    found = alerts(world)
    assert len(found) == 1 and "runs claude-fable-5-2" in found[0] and "asked for claude-sonnet-5-5." in found[0]


def test_model_names_match_by_id_or_short_name():
    assert orch_guards.model_ok("opus", "claude-opus-5-5")
    assert orch_guards.model_ok("claude-opus-4-8", "claude-opus-4-8[1m]")
    assert orch_guards.model_ok("opus-5-5", "claude-opus-5-5")
    assert not orch_guards.model_ok("opus", "claude-fable-5-2")
    assert not orch_guards.model_ok("claude-opus-5-5", "claude-opus-5")


def test_child_with_no_build_and_no_model_asked_gets_no_model_check(world):
    plain = {"initiation": "user_asked", "title": "Read", "cwd": REPO}
    orchestrator(world, [*started("t1", plain, "local_c1", 3990)])
    child(world, "local_c1", "claude-fable-5-2", REPO)
    assert alerts(world) == []


# ---------- folder guard (real failure: 2026-11-10 vault cards started in the voice folder) ----------

VAULT_CARD = {"initiation": "own_initiative", "title": "Vault review",
              "prompt": f"Before any work, call `mcp__ccd_directory__change_directory` to `{VAULT}`. "
                        "If that fails, stop and say so."}


def test_card_whose_child_never_moved_to_the_vault_alerts_until_it_moves(world):
    orchestrator(world, [*proposed("t1", VAULT_CARD, "task_v1", 3990)])
    child(world, "local_c1", "claude-opus-5-5", VOICE, task_id="task_v1", originCwd=VOICE)
    found = alerts(world)
    assert len(found) == 1
    assert f"works in {VOICE}, but [Voice](#local_orch) asked for {VAULT}" in found[0]
    world.append("cli-local_c1", ran("claude-opus-5-5", VAULT, 30))
    (world.orch / "state.json").unlink()
    assert alerts(world) == []


def test_start_with_cwd_accepts_a_worktree_under_it_and_alerts_on_another_repo(world):
    other = {**BUILD, "title": "Other"}
    orchestrator(world, [*started("t1", BUILD, "local_c1", 3990), *started("t2", other, "local_c2", 3980)])
    child(world, "local_c1", "claude-opus-5-5", f"{REPO}/.claude/worktrees/pr3", originCwd=REPO)
    child(world, "local_c2", "claude-opus-5-5", "/p/repo-two", originCwd="/p/repo-two")
    found = alerts(world)
    assert len(found) == 1 and "[Child local_c2](#local_c2) works in /p/repo-two" in found[0]


def test_card_with_no_folder_must_start_in_the_orchestrator_folder(world):
    card = {"initiation": "own_initiative", "title": "Plain card", "prompt": "Count the notes."}
    orchestrator(world, [*proposed("t1", card, "task_a", 3990), *proposed("t2", card, "task_b", 3980)], cwd=VAULT)
    child(world, "local_c1", "claude-opus-5-5", VAULT, task_id="task_a")
    child(world, "local_c2", "claude-opus-5-5", VOICE, task_id="task_b")
    found = alerts(world)
    assert len(found) == 1 and "local_c2" in found[0] and f"asked for {VAULT}" in found[0]


def test_orchestrator_in_a_worktree_accepts_children_in_sibling_worktrees(world):
    card = {"initiation": "own_initiative", "title": "Fix", "prompt": "Fix it."}
    orchestrator(world, [*proposed("t1", card, "task_a", 3990)], cwd=f"{REPO}/.claude/worktrees/orch")
    child(world, "local_c1", "claude-opus-5-5", f"{REPO}/.claude/worktrees/fix", task_id="task_a", originCwd=REPO)
    assert alerts(world) == []


def test_new_child_gets_time_to_move_before_the_folder_check(world):
    orchestrator(world, [*proposed("t1", VAULT_CARD, "task_v1", 120)])
    child(world, "local_c1", "claude-opus-5-5", VOICE, ago=100, task_id="task_v1")
    assert alerts(world) == []


def test_card_started_by_its_proposal_id_is_checked_against_the_card(world):
    start = {"initiation": "user_asked", "proposal_id": "task_v1"}
    orchestrator(world, [*proposed("t1", VAULT_CARD, "task_v1", 3990), *started("t2", start, "local_c1", 3900)])
    child(world, "local_c1", "claude-opus-5-5", VOICE)  # no taskId in its record: the session id links it
    assert len(alerts(world)) == 1


def test_failed_starts_other_parents_archived_and_old_children_are_not_checked(world):
    failed = [tool("t1", "mcp__ccd_session__start_session", BUILD, 3990), result("t1", "Error: No local branch", 3989)]
    orchestrator(world, [*failed, *started("t2", BUILD, "local_c2", 3980), *started("t3", BUILD, "local_c3", 3970)])
    child(world, "local_c1", "claude-fable-5-2", "/x")
    child(world, "local_c2", "claude-fable-5-2", "/x", isArchived=True)
    child(world, "local_c3", "claude-fable-5-2", "/x", ago=3 * 86400)
    world.session("local_free", "cli-free", "Free", [ran("claude-fable-5-2", "/x", 60)], model="claude-fable-5-2")
    assert alerts(world) == []


def test_starts_are_read_from_new_lines_only(world):
    orchestrator(world, [*started("t1", BUILD, "local_c1", 3990)])
    child(world, "local_c1", "claude-opus-5-5", REPO)
    orch_state.build()
    offset = json.loads((world.orch / orch_state.CACHE_FILE).read_text())["guards"]["starts"]["local_orch"]["offset"]
    world.append("cli-orch", *started("t2", {**BUILD, "title": "Two"}, "local_c2", 100))
    child(world, "local_c2", "claude-fable-5-2", REPO)
    (world.orch / "state.json").unlink()
    found = alerts(world)
    cache = json.loads((world.orch / orch_state.CACHE_FILE).read_text())["guards"]["starts"]["local_orch"]
    assert cache["offset"] > offset and len(cache["starts"]) == 2
    assert len(found) == 1 and "local_c2" in found[0]


# ---------- real failure: a stalled child session ----------

def test_stalled_children_are_flagged_with_or_without_a_pr(world):
    orchestrator(world, [user("hello", 100)])
    day = 86400
    child(world, "local_pr", "claude-opus-5-5", REPO, ago=3 * day, title="Plans 1961",
          lastActivityAt=int((NOW - 30 * 3600) * 1000), prs=[{"prNumber": 1961, "repo": "org/r", "state": "OPEN"}])
    child(world, "local_nopr", "claude-opus-5-5", REPO, ago=3 * day, title="Plans 1628",
          lastActivityAt=int((NOW - 30 * 3600) * 1000), postTurnSummary={"status_category": "review_ready"})
    child(world, "local_done", "claude-opus-5-5", REPO, ago=3 * day, title="Done",
          lastActivityAt=int((NOW - 30 * 3600) * 1000), postTurnSummary={"status_category": "completed"})
    child(world, "local_busy", "claude-opus-5-5", REPO, ago=3 * day, title="Busy")
    idle = facts_of(orch_state.build(), "idle_pr")
    assert len(idle) == 2
    assert any("'Plans 1961'" in text and "org/r#1961" in text for text in idle)
    assert any("[Plans 1628](#local_nopr)" in text and "status review_ready, no open PR" in text for text in idle)


# ---------- real failure: re-asking a settled question after compaction ----------

def test_question_answered_before_compaction_is_not_open_after_it(world):
    ask = {"questions": [{"question": "Merge PR 5?", "options": [{"label": "Yes"}, {"label": "No"}]}]}
    world.session("local_kid", "cli-kid", "Kid", [
        user("build it", 7200), tool("q1", "AskUserQuestion", ask, 7000), result("q1", "User answered: Yes", 6000),
        say("Merged.", 5900)], spawnedFrom={"sessionId": "local_orch"}, cwd=REPO,
        createdAt=int((NOW - 7200) * 1000))
    orchestrator(world, [user("yes, merge PR 5", 6100), tool("s1", "SendMessage", {"to": "local_kid", "message": "Yes"}, 6050),
                         *compaction(3000), user("what is open?", 1200), say("Nothing.", 1190)])
    out = orch_hook.output("start", "cli-orch")
    assert "QUESTIONS OPEN IN OTHER SESSIONS" not in out and "Merge PR 5?" not in out
    assert '"yes, merge PR 5": action: SendMessage' in out  # the settled answer is back in front of the model


# ---------- real failure: a stale vault (working days, not calendar days) ----------

def test_vault_is_stale_only_after_a_full_working_day(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("x")
    monday = dt.datetime(2026, 11, 9, 9, 0).timestamp()
    for day, stale in ((dt.datetime(2026, 11, 6, 17, 0), False), (dt.datetime(2026, 11, 5, 17, 0), True)):
        os.utime(note, (day.timestamp(), day.timestamp()))
        text = orch_state.vault_facts(str(vault), monday)[0]["text"]
        assert text.startswith("Vault stale since 11-05") if stale else text.startswith("Vault newest note 11-06")


# ---------- real failure: a murmur relayed as a decision ----------

@pytest.mark.parametrize("murmur", ["Mm-hmm", "mm-hmm.", "Mhm", "Hmm", "uh-huh"])
def test_murmur_relayed_as_a_decision_alerts(world, murmur):
    orchestrator(world, [user(murmur, 1200),
                         tool("s1", "SendMessage", {"to": "local_kid", "message": "The user approves"}, 1190)])
    found = alerts(world)
    assert len(found) == 1 and "as a decision (SendMessage)" in found[0] and "ask again" in found[0]


def test_murmur_with_no_relay_and_real_words_with_a_relay_give_no_alert(world):
    orchestrator(world, [user("Mm-hmm", 1500), say("Noted.", 1490),
                         user("yes, merge it", 1200), tool("s1", "SendMessage", {"to": "local_kid", "message": "merge"}, 1190)])
    state = orch_state.build()
    assert facts_of(state, "alert") == []


# ---------- compatibility ----------

def test_main_hook_reads_a_state_with_guard_facts(world, tmp_path):
    orchestrator(world, [user("Mm-hmm", 1200), tool("s1", "SendMessage", {"to": "local_kid", "message": "ok"}, 1190),
                         *started("t1", BUILD, "local_c1", 3990)])
    child(world, "local_c1", "claude-fable-5-2", REPO)
    state = orch_state.build()
    assert {f["kind"] for f in state["facts"]} <= set(orch_state.PRIORITY) and state["version"] == 1
    old = main_code(tmp_path)
    hook = subprocess.run([sys.executable, str(old / "orch_hook.py"), "start", "--session", "local_orch",
                           "--limit", "0.001"], env=world.env(), capture_output=True, text=True, check=False)
    assert hook.returncode == 0 and "FAILED" not in hook.stdout and "runs claude-fable-5-2" in hook.stdout, hook.stdout


# ---------- a guard fault never fails the build ----------

def test_a_guard_that_raises_gives_one_alert_and_the_next_build_recovers(world, monkeypatch):
    orchestrator(world, [user("merge it", 1200), say("ok", 1190), *started("t1", BUILD, "local_c1", 3990)])
    child(world, "local_c1", "claude-fable-5-2", REPO)

    def broken(*args):
        raise OSError("transcript vanished")

    real = orch_guards.scan_starts
    monkeypatch.setattr(orch_guards, "scan_starts", broken)
    state = orch_state.build()
    assert facts_of(state, "alert") == ["Guards failed: OSError: transcript vanished. The other facts are fine."]
    assert state["last_turns"]  # the rest of the state is still built
    assert "guards" not in json.loads((world.orch / orch_state.CACHE_FILE).read_text())
    monkeypatch.setattr(orch_guards, "scan_starts", real)  # not undo(): that also drops the temp folders
    assert orch_state.state_dir() == str(world.orch)
    (world.orch / "state.json").unlink()
    found = alerts(world)
    assert len(found) == 1 and "runs claude-fable-5-2" in found[0]


def test_a_malformed_cached_guards_part_is_dropped(world):
    orchestrator(world, [*started("t1", BUILD, "local_c1", 3990)])
    child(world, "local_c1", "claude-fable-5-2", REPO)
    orch_state.build()
    path = world.orch / orch_state.CACHE_FILE
    cache = json.loads(path.read_text())
    cache["guards"] = {"starts": {"local_orch": {"path": 5, "offset": "x"}}, "tails": []}
    path.write_text(json.dumps(cache))
    (world.orch / "state.json").unlink()
    first = alerts(world)
    assert len(first) == 1 and first[0].startswith("Guards failed: AttributeError:")
    (world.orch / "state.json").unlink()
    found = alerts(world)
    assert len(found) == 1 and "runs claude-fable-5-2" in found[0]

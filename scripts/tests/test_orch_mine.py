"""The decision and correction miners, and the decisions in the start digest.

Every test uses temp folders (VOZ_ORCH_DIR) and fake transcripts. No test reads
or writes ~/.claude or the Desktop app's records.
"""

import ast
import os
import subprocess
import sys

import orch_hook
import orch_mine
import orch_state
import pytest
from test_orch_state import NOW, SCRIPTS, World, iso, say, tool, user

# The main branch when this miner was written (PR 7). Its hook must keep working in a folder this code wrote.
MAIN_COMMIT = "b37eaec616edbcce72368d2dcd2aaaa66e7cb378"


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def spoken(text, ago):
    """A turn the voice plugin submits for the user, as it shows in the transcript."""
    return {"type": "user", "timestamp": iso(ago), "origin": {"kind": "plugin", "name": "voz", "asUser": True},
            "message": {"role": "user", "content": text}}


def voice_note(ago):
    return {"type": "user", "isMeta": True, "timestamp": iso(ago), "origin": {"kind": "plugin", "name": "voz"},
            "message": {"content": [{"type": "text", "text": '{"source":"voz-runtime","voiceState":"listening"}'}]}}


def relay(text, ago):
    return {"type": "user", "isMeta": True, "timestamp": iso(ago), "origin": {"kind": "peer", "name": "Voice", "body": text},
            "message": {"content": text}}


def answered(question, answer, ago):
    return {"type": "user", "timestamp": iso(ago), "toolUseResult": {"questions": [], "answers": {question: answer}},
            "message": {"content": [{"type": "tool_result", "tool_use_id": "q1", "content": "The user answered."}]}}


def mined(world, entries):
    world.session("local_orch", "cli-orch", "Voice", entries)
    world.orchestrators(["local_orch"])
    state = orch_state.build()
    return state, orch_mine.read_log(orch_mine.DECISIONS_FILE), orch_mine.read_log(orch_mine.CANDIDATES_FILE)


def words_of(items):
    return [item["words"] for item in items]


# ---------- decisions ----------

def test_typed_approval_keeps_question_session_time_and_source(world):
    _, decisions, _ = mined(world, [say("PR 7 is green. Merge PR 7?", 400), user("Yes, merge it", 300)])
    assert len(decisions) == 1
    item = decisions[0]
    assert (item["kind"], item["words"], item["question"], item["channel"]) == ("approve", "Yes, merge it", "PR 7 is green. Merge PR 7?", "typed")
    assert item["session"] == "local_orch" and item["source"] == "transcript cli-orch.jsonl"
    assert abs(item["at"] - (NOW - 300)) < 2


def test_short_question_keeps_the_sentence_before_it(world):
    _, decisions, _ = mined(world, [
        say("Draft for the review session:\n> I'll ask Concierge to post a card for the retest. Send?", 500),
        user("Please send it.", 490),
        say("PR 7 is green and the checks passed. Do you want me to merge PR 7 now?", 400),
        user("Yes.", 390),
    ])
    assert [d["question"] for d in decisions] == [
        "I'll ask Concierge to post a card for the retest. Send?",
        "Do you want me to merge PR 7 now?"]  # six words or more: the question alone


def test_short_question_with_a_long_sentence_before_it_is_cut_to_120_characters(world):
    _, decisions, _ = mined(world, [say("This message " + "tells the team a lot of things " * 8 + "today. Send?", 500),
                                    spoken("Yes.", 490)])
    question = decisions[0]["question"]
    assert len(question) == 120 and question.startswith("…") and question.endswith("today. Send?")


def test_spoken_yes_answers_the_question_and_a_murmur_is_not_a_decision(world):
    _, decisions, _ = mined(world, [
        say("Should I send the draft to Sam?", 500), voice_note(499), spoken("Mm-hmm.", 498),
        voice_note(497), spoken("Yes.", 496),
    ])
    assert [(d["words"], d["question"], d["channel"]) for d in decisions] == [("Yes.", "Should I send the draft to Sam?", "spoken")]


def test_relayed_voice_aside_and_voice_notes_are_not_decisions(world):
    _, decisions, candidates = mined(world, [
        say("Merge it?", 500), relay("The user said aloud: yes, merge it. You forgot the link again.", 490),
        voice_note(480),
    ])
    assert decisions == [] and candidates == []


@pytest.mark.parametrize("before, words", [
    (None, "Yes."),                                  # no question before it
    ("What are the plans for the other two?", "Yeah."),  # "yes" does not settle a what-question
    ("Merge it?", "Can you merge it?"),              # a request is not a decision
    ("Send it?", "Yeah, could you maybe put together a list of all the things first and then we look again"),
])
def test_words_that_settle_nothing_are_not_decisions(world, before, words):
    entries = ([say(before, 400)] if before else []) + [user(words, 300)]
    _, decisions, _ = mined(world, entries)
    assert decisions == []


@pytest.mark.parametrize("words, kind", [
    ("Yes, use those", "approve"), ("Yes, do that", "approve"), ("go ahead", "approve"),
    ("No, don't merge it yet", "reject"), ("Not now", "reject"), ("Let's go with the second one", "choose"),
])
def test_explicit_approve_reject_and_choose_need_no_question(world, words, kind):
    _, decisions, _ = mined(world, [user(words, 300)])
    assert [(d["words"], d["kind"]) for d in decisions] == [(words, kind)]


def test_wrap_up_ask_is_the_question_and_question_tool_answers_are_choices(world):
    _, decisions, _ = mined(world, [
        tool("w1", "mcp__ccd_turn__wrap_up", {"recap": "Done.", "ask": "Merge PR 9?", "actions": [{"label": "Yes"}]}, 600),
        user("Yes", 590),
        tool("q1", "AskUserQuestion", {"questions": [{"question": "Which model?", "options": []}]}, 500),
        answered("Which model?", "Sonnet (Recommended)", 490),
        answered("Skip?", "[User did not answer this question]", 480),
    ])
    assert [(d["words"], d["question"], d["kind"], d["channel"]) for d in decisions] == [
        ("Yes", "Merge PR 9?", "approve", "typed"),
        ("Sonnet (Recommended)", "Which model?", "choose", "question tool")]


# ---------- corrections ----------

def test_correction_phrases_become_rule_candidates(world):
    _, _, candidates = mined(world, [
        say("I opened PR 3 against the feature branch.", 900),
        user("Again the main is the main. PRs go to main.", 800),
        user("you forgot the link", 700),
        user("I told you to check the inbox first", 600),
        user("Stop.", 500),
        user("stop the server please", 400),
        user("(pasted text:) never send this (end of paste)", 300),
    ])
    assert words_of(candidates) == ["Again the main is the main. PRs go to main.", "you forgot the link",
                                     "I told you to check the inbox first", "Stop."]
    assert candidates[0]["phrases"] == ["again"] and candidates[0]["after"].startswith("I opened PR 3")


# ---------- incremental, no duplicates ----------

def test_second_build_reads_only_new_lines_and_a_lost_cache_adds_no_duplicates(world):
    mined(world, [say("Merge it?", 400), user("Yes, merge it", 300)])
    world.append("cli-orch", say("Send it?", 200), user("Yes, send it", 100))
    orch_state.build(started=orch_state.now_s() + 1)
    assert words_of(orch_mine.read_log(orch_mine.DECISIONS_FILE)) == ["Yes, merge it", "Yes, send it"]
    os.unlink(world.orch / orch_mine.MINE_CACHE)
    orch_state.build(started=orch_state.now_s() + 1)
    assert words_of(orch_mine.read_log(orch_mine.DECISIONS_FILE)) == ["Yes, merge it", "Yes, send it"]
    assert not list(world.orch.glob(".tmp-*"))


def test_broken_log_is_an_alert_and_the_build_goes_on(world):
    world.orch.mkdir()
    (world.orch / orch_mine.DECISIONS_FILE).write_text("{broken\n")
    world.session("local_orch", "cli-orch", "Voice", [user("Yes, merge it", 300)])
    world.orchestrators(["local_orch"])
    state = orch_state.build()
    alerts = [f["text"] for f in state["facts"] if f["kind"] == "alert"]
    assert any("decision miner failed" in text and "line 1" in text for text in alerts)
    assert (world.orch / orch_mine.DECISIONS_FILE).read_text() == "{broken\n"  # the user's log is never rewritten blind


# ---------- the start digest ----------

def test_start_digest_shows_newest_decisions_below_the_facts_in_budget(world):
    entries = []
    for number in range(40):
        entries += [say(f"Merge PR {number}?", 4000 - number * 20), user(f"Yes, merge PR {number} " + "now " * 30, 3990 - number * 20)]
    state, _, _ = mined(world, entries)
    assert len(state["decisions"]) == orch_mine.KEEP_IN_STATE
    out = orch_hook.output("start", "local_orch")
    assert orch_state.DECISIONS_HEADING in out
    block = out.split(orch_state.DECISIONS_HEADING, 1)[1]
    assert "PR 39" in block.splitlines()[1]  # newest first
    assert orch_state.tokens(orch_state.decisions_block(state, orch_state.DECISIONS_BUDGET)) <= orch_state.DECISIONS_BUDGET
    assert orch_state.tokens(out) <= orch_state.BUDGETS["start"]
    digest = (world.orch / "digest.md").read_text()
    assert digest.index("STATE as of") < digest.index(orch_state.DECISIONS_HEADING)


def test_turn_digest_has_no_decisions_and_a_new_decision_does_not_reprint_it(world):
    mined(world, [say("Merge it?", 400), user("Yes, merge it", 300)])
    first = orch_hook.output("turn", "local_orch")
    assert orch_state.DECISIONS_HEADING not in first
    world.append("cli-orch", say("Send it?", 200), user("Yes, send it", 190))
    orch_state.build(started=orch_state.now_s() + 1)
    assert "no change" in orch_hook.output("turn", "local_orch")


# ---------- rules.md import: dry run only ----------

def test_import_dry_run_prints_lines_and_writes_nothing(world, capsys):
    mined(world, [user("you forgot the link", 700), user("you forgot the link", 600), user("I told you to use Eastern time", 500)])
    rules = world.orch / "rules.md"
    rules.write_text('- 2026-11-01: "I told you to use Eastern time"\n')
    before = {path.name: path.read_bytes() for path in world.orch.iterdir() if path.is_file()}
    assert orch_mine.main(["import-rules", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing written. An import would add 1 line(s)" in out
    assert '"you forgot the link" (you forgot; typed, local_orch; seen 2 times)' in out
    assert "Eastern" not in out.split("\n", 1)[1]
    assert {path.name: path.read_bytes() for path in world.orch.iterdir() if path.is_file()} == before
    assert orch_mine.main(["import-rules"]) == 2


def test_mine_command_needs_a_build_first(world, capsys):
    assert orch_mine.main(["mine"]) == 2
    assert "Run: python3 scripts/orch_state.py build" in capsys.readouterr().err


# ---------- rollback: the main-branch code in a folder this code wrote ----------

def main_scripts(tmp_path):
    folder = tmp_path / "main_scripts"
    folder.mkdir()
    for name in ("orch_state.py", "orch_hook.py", "ledger.py", "session_index.py"):
        shown = subprocess.run(["git", "-C", str(SCRIPTS), "show", f"{MAIN_COMMIT}:scripts/{name}"],
                               capture_output=True, text=True, check=False)
        if shown.returncode != 0:
            pytest.skip(f"commit {MAIN_COMMIT} is not in this clone")
        (folder / name).write_text(shown.stdout)
    return folder


def test_main_hook_works_in_a_folder_with_decisions(world, tmp_path):
    mined(world, [say("Merge it?", 400), user("Yes, merge it", 300)])
    assert orch_state.load_state()["decisions"]
    old = main_scripts(tmp_path)
    for mode in ("start", "turn"):
        hook = subprocess.run([sys.executable, str(old / "orch_hook.py"), mode, "--session", "local_orch"],
                              env=world.env(), capture_output=True, text=True, check=False)
        assert hook.returncode == 0 and hook.stdout.startswith("STATE as of ") and "FAILED" not in hook.stdout, hook.stdout
    built = subprocess.run([sys.executable, str(old / "orch_state.py"), "build"], env=world.env(), capture_output=True, text=True, check=False)
    assert built.returncode == 0, built.stderr
    state = orch_state.build(started=orch_state.now_s() + 1)
    assert words_of(state["decisions"]) == ["Yes, merge it"]


# ---------- Python 3.9, standard library only ----------

def test_miner_uses_only_the_standard_library():
    tree = ast.parse((SCRIPTS / "orch_mine.py").read_text())
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported <= {"__future__", "argparse", "contextlib", "datetime", "fcntl", "hashlib", "json", "os", "re", "sys",
                        "ledger", "orch_state"}, imported


@pytest.mark.skipif(not os.path.exists("/usr/bin/python3"), reason="no system Python on this machine")
def test_miner_runs_on_system_python(world):
    mined(world, [say("Merge it?", 400), user("Yes, merge it", 300)])
    run = subprocess.run(["/usr/bin/python3", str(SCRIPTS / "orch_mine.py"), "decisions"], env=world.env(),
                         capture_output=True, text=True, timeout=60, check=False)
    assert run.returncode == 0 and "approve [typed]: Yes, merge it (to: Merge it?)" in run.stdout, run.stderr
    built = subprocess.run(["/usr/bin/python3", str(SCRIPTS / "orch_state.py"), "build"], env=world.env(),
                           capture_output=True, text=True, timeout=60, check=False)
    assert built.returncode == 0, built.stderr

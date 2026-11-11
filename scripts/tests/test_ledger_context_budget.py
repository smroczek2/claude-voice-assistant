"""Context efficiency must preserve complete task coverage and decision text."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ledger


@pytest.fixture
def book():
    entries = {}
    for i in range(12):
        sid = f"local_00000000-0000-0000-0000-{i:012d}"
        entries[sid] = {
            "session_id": sid, "nickname": f"task {i}", "title": f"Task {i}",
            "status": "needs you" if i < 10 else "idle", "watched": True,
            "what": "Task purpose " * 200, "standing": 'Latest report "quoted" ' * 200,
            "needs_action": "Review the selected task.", "last_activity": 2000 - i,
            "question": {"text": "Ready to proceed? " * 200, "at": "2026-11-06T10:00:00Z",
                         "choices": [{"label": f"Choice {j} " * 30, "means": "Details " * 200} for j in range(8)],
                         "recap": "Long recap " * 200, "brief": "Long brief " * 200},
            "last_words": {"from": "user", "text": "Wait for my approval.", "at": "2026-11-06T10:02:00Z"},
            "said": [{"text": "Do not merge it yet.", "at": 2000}],
            "prs": [], "aliases": [], "started_with": "Full original task.",
            "last_answer": "Full report " * 200,
        }
    return {"entries": entries, "refreshed_at": 12345}


def run_cli(monkeypatch, capsys, book, *args):
    # Storage/refresh are the external boundary; argument parsing and output are real.
    monkeypatch.setattr(ledger, "load_json", lambda *a: book)
    monkeypatch.setattr(ledger, "refresh", lambda current, *a: current)
    monkeypatch.setattr(sys, "argv", ["ledger.py", *args])
    try:
        result = ledger.main()
    except SystemExit as error:
        result = error.code
    output = capsys.readouterr()
    return result, output


def test_default_snapshot_includes_every_waiting_task(monkeypatch, capsys, book):
    code, output = run_cli(monkeypatch, capsys, book, "snapshot")
    assert code == 0, output.err
    page = json.loads(output.out)
    assert page["total"] == 10
    assert page["refreshed_at"] == 12345
    assert "local" in page["source"].lower()
    expected = {e["session_id"] for e in book["entries"].values() if e["status"] == "needs you"}
    assert {e["session_id"] for e in page["items"]} == expected
    assert len(page["items"]) == 10
    assert page["has_more"] is False
    assert page["limit"] is None
    first = page["items"][0]
    assert first["session_id"] == next(iter(book["entries"]))
    assert first["status"] == "needs you"
    assert first["last_words"]["text"] == "Wait for my approval."
    assert first["said"]["text"] == "Do not merge it yet."
    assert first["question"]["choice_count"] == 8
    assert first["question"]["text"] == book["entries"][first["session_id"]]["question"]["text"]


def test_old_waiting_task_is_not_hidden_by_recent_activity(monkeypatch, capsys, book):
    sid = list(book["entries"])[9]
    book["entries"][sid].update(last_activity=1, title="Older urgent decision")
    code, output = run_cli(monkeypatch, capsys, book, "snapshot")
    assert code == 0, output.err
    assert any(e["session_id"] == sid for e in json.loads(output.out)["items"])


def test_decision_choices_and_user_directions_are_not_cut(monkeypatch, capsys, book):
    sid = next(iter(book["entries"]))
    entry = book["entries"][sid]
    entry["last_words"]["text"] = "Background " * 60 + "Do not merge."
    entry["said"][-1]["text"] = "More background " * 60 + "Wait until Monday."
    code, output = run_cli(monkeypatch, capsys, book, "where", sid, "--compact")
    assert code == 0, output.err
    item = json.loads(output.out)["items"][0]
    assert item["question"]["choices"] == entry["question"]["choices"]
    assert item["question"]["text"] == entry["question"]["text"]
    assert item["last_words"] == entry["last_words"]
    assert item["said"] == entry["said"][-1]
    assert item["background_in_full_detail"] is True
    code, output = run_cli(monkeypatch, capsys, book, "where", sid)
    assert code == 0, output.err
    assert entry["question"]["brief"] in output.out
    assert entry["question"]["recap"] in output.out
    assert entry["last_words"]["text"] in output.out


def test_snapshot_paginates_and_all_statuses_are_available(monkeypatch, capsys, book):
    code, output = run_cli(monkeypatch, capsys, book, "snapshot", "--status", "all", "--limit", "3", "--offset", "9")
    assert code == 0, output.err
    page = json.loads(output.out)
    assert page["total"] == 12
    assert page["offset"] == 9
    assert page["has_more"] is False
    assert [row["title"] for row in page["items"]] == ["Task 9", "Task 10", "Task 11"]


def test_targeted_compact_read_keeps_identity_and_full_detail_remains(monkeypatch, capsys, book):
    sid = next(iter(book["entries"]))
    code, output = run_cli(monkeypatch, capsys, book, "where", sid, "--compact")
    assert code == 0, output.err
    item = json.loads(output.out)["items"][0]
    assert item["session_id"] == sid
    code, output = run_cli(monkeypatch, capsys, book, "where", sid)
    assert code == 0
    assert "Full original task." in output.out
    assert "choice: Choice 7" in output.out


@pytest.mark.parametrize("args", [("--limit", "0"), ("--offset", "-1")])
def test_snapshot_rejects_invalid_explicit_pages(monkeypatch, capsys, book, args):
    code, output = run_cli(monkeypatch, capsys, book, "snapshot", *args)
    assert code == 2
    assert "range" in output.err.lower()


def test_explicit_page_size_is_not_capped_at_ten(monkeypatch, capsys, book):
    code, output = run_cli(monkeypatch, capsys, book, "snapshot", "--status", "all", "--limit", "12")
    assert code == 0, output.err
    page = json.loads(output.out)
    assert len(page["items"]) == 12 and not page["has_more"]


def test_empty_snapshot_is_explicit_not_a_failure(monkeypatch, capsys):
    code, output = run_cli(monkeypatch, capsys, {"entries": {}, "refreshed_at": 123}, "snapshot")
    assert code == 0, output.err
    page = json.loads(output.out)
    assert page["items"] == [] and page["total"] == 0 and page["has_more"] is False


def test_user_name_comes_from_config(tmp_path, monkeypatch):
    import json as _json
    config = tmp_path / "config.json"
    config.write_text(_json.dumps({"user_name": "Alex"}))
    monkeypatch.setattr(ledger, "CONFIG_PATH", str(config))
    assert ledger.user_name() == "Alex"
    assert ledger.possessive() == "Alex's"
    config.write_text("{}")
    assert ledger.possessive() == "The user's"
    assert ledger.load_config()["routine_titles"] == []

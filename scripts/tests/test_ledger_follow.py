"""Watcher output must not wake the conversation that owns voice itself."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ledger
from session_index import SessionIndex


class EndWatch(Exception):
    pass


def test_follow_skips_own_alerts_but_keeps_every_worker_and_snapshot(monkeypatch, tmp_path, capsys):
    store = tmp_path / "store"
    store.mkdir()
    (store / "voz_inline-test.json").write_text(json.dumps({"voz.assistant": "cli-self"}))
    monkeypatch.setattr(ledger, "VOICE_STORE_DIR", str(store), raising=False)
    monkeypatch.setattr(ledger, "load_session_index", lambda: SessionIndex(
        by_cli={"cli-self": {"sessionId": "local_self"}}))
    before = {"entries": {}}
    after = {"entries": {
        "local_self": {"session_id": "local_self", "nickname": "renamed voice chat", "title": "Reload plugins",
                       "status": "needs you", "question": {"text": "Anything else?", "at": "new"}},
        "local_worker": {"session_id": "local_worker", "nickname": "worker", "title": "Worker",
                         "status": "needs you", "question": {"text": "Merge it?", "at": "new"}},
        "local_finished": {"session_id": "local_finished", "nickname": "finished", "title": "Finished",
                           "status": "finished", "interest": True, "standing": "All complete."},
    }}
    states = iter([copy.deepcopy(before), copy.deepcopy(after)])
    monkeypatch.setattr(ledger, "refresh", lambda *_: next(states))
    monkeypatch.setattr(ledger, "load_json", lambda *_: {})
    sleeps = iter([None])

    def sleep(_):
        if next(sleeps, "end") == "end":
            raise EndWatch

    monkeypatch.setattr(ledger.time, "sleep", sleep)
    with pytest.raises(EndWatch):
        ledger.follow(1)
    assert capsys.readouterr().out.splitlines() == ["NEEDS_YOU worker: Merge it?", "FINISHED finished: All complete."]
    page = ledger.snapshot(after, list(after["entries"].values()))
    assert page["total"] == 3
    assert {e["session_id"] for e in page["items"]} == set(after["entries"])


@pytest.mark.parametrize("owner", ["cli-self", "local_self"])
def test_owner_identity_uses_store_not_chat_title(monkeypatch, tmp_path, owner):
    (tmp_path / "voz_inline-test.json").write_text(json.dumps({"voz.assistant": owner}))
    (tmp_path / "voz_incomplete.json").write_text("{")
    (tmp_path / "other.json").write_text(json.dumps({"voz.assistant": "cli-worker"}))
    monkeypatch.setattr(ledger, "VOICE_STORE_DIR", str(tmp_path))
    monkeypatch.setattr(ledger, "load_session_index", lambda: SessionIndex(
        by_cli={"cli-self": {"sessionId": "local_self"}}))
    assert "local_self" in ledger.voice_session_ids()
    assert "cli-worker" not in ledger.voice_session_ids()


def test_follow_rechecks_owner_after_takeover(monkeypatch, capsys):
    owners = iter([{"a"}, {"b"}])
    monkeypatch.setattr(ledger, "voice_session_ids", lambda: next(owners))
    monkeypatch.setattr(ledger, "load_json", lambda *_: {})
    count = 0

    def refresh(_):
        nonlocal count
        count += 1
        return {"entries": {sid: {"session_id": sid, "nickname": sid, "status": "needs you",
                                    "question": {"text": "Ready?", "at": str(count)}} for sid in ("a", "b")}}

    def sleep(_):
        if count >= 3:
            raise EndWatch

    monkeypatch.setattr(ledger, "refresh", refresh)
    monkeypatch.setattr(ledger.time, "sleep", sleep)
    with pytest.raises(EndWatch):
        ledger.follow(1)
    assert capsys.readouterr().out.splitlines() == ["NEEDS_YOU b: Ready?", "NEEDS_YOU a: Ready?"]

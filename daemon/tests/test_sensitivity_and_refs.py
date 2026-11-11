"""Ported voice fixes: the sensitivity setting moves the room-level margin, and private
reference voices in ~/.voz/refs are offered for cloning."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from voz.config import DEFAULTS, load_config
from voz.daemon import Daemon
from voz.speech import Fala


@pytest.mark.parametrize("level, start, min_ms, margin", [("baixa", 0.6, 250, 10.0), ("media", 0.45, 150, 7.0),
                                                          ("alta", 0.3, 60, 4.0)])
def test_sensitivity_sets_the_noise_margin_with_the_thresholds(level, start, min_ms, margin):
    async def scenario():
        daemon = Daemon(load_config(), 0)
        daemon.out = lambda kind, **payload: None
        daemon.ouvido = SimpleNamespace(recalc=lambda: None, set_ptt_mode=lambda on: None)
        daemon.fala = SimpleNamespace(voz_actual=lambda: "turbo:default")
        result = await daemon._definir({"sensitivity": level})
        vad = daemon.cfg["vad"]
        assert (vad["start_threshold"], vad["min_speech_ms"], vad["noise_margin_db"]) == (start, min_ms, margin)
        assert result["sensitivity"] == level
    asyncio.run(scenario())


def test_default_config_has_the_noise_gate_values():
    vad = load_config()["vad"]
    assert vad["noise_margin_db"] == 8.0 and vad["min_level_db"] == -55.0


def test_default_pause_before_sending_is_2500_ms(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty-home"))
    assert DEFAULTS["vad"]["turn_gap_ms"] == 2500
    assert load_config()["vad"]["turn_gap_ms"] == 2500  # the engine daemon/config.toml


def test_private_reference_voices_are_listed_after_the_recorded_one(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".voz" / "refs").mkdir(parents=True)
    for name in ("ref.wav", "refs/b.wav", "refs/a.wav", "refs/notes.txt"):
        (tmp_path / ".voz" / name).write_bytes(b"RIFF")
    refs = [r for r in Fala(load_config()).refs() if tmp_path in r.parents]
    assert [r.relative_to(tmp_path / ".voz").as_posix() for r in refs] == ["ref.wav", "refs/a.wav", "refs/b.wav"]


def test_no_private_reference_folder_is_fine(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert [r for r in Fala(load_config()).refs() if tmp_path in r.parents] == []

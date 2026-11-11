"""Control boundaries must discard capture work already awaiting another stage."""
import asyncio
import importlib
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from voz.config import load_config
from voz.daemon import Daemon


async def noop(*args):
    return None


def daemon_fixture():
    cfg = load_config()
    cfg["vad"]["turn_gap_ms"] = 0
    cfg["ui"]["ack_sound"] = False
    cfg["keywords"]["dynamic"] = False
    daemon = Daemon(cfg, 0)
    daemon.loop = asyncio.get_running_loop()
    daemon.events = asyncio.Queue()
    daemon.ouvido = SimpleNamespace(sr=16000, set_muted=lambda on: None, stop=lambda: None)
    daemon.fala = SimpleNamespace(stop=noop, skip=noop, paused=False, queue=asyncio.Queue(), _play_q=asyncio.Queue(), busy=False)
    emitted = []
    daemon.out = lambda kind, **payload: emitted.append((kind, payload))
    daemon._limpar = lambda text: asyncio.sleep(0, result=text)
    return daemon, emitted


@pytest.mark.parametrize("stage", ["retranscription", "cleanup"])
def test_pause_resume_discards_inflight_final_turn(stage):
    async def scenario():
        daemon, emitted = daemon_fixture()
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_stage(value):
            started.set()
            await release.wait()
            return {"text": "Stale words from before pause."} if stage == "retranscription" else value

        daemon._acumulado = "Stale words from before pause."
        if stage == "retranscription":
            daemon._segmentos = [np.zeros(16000), np.zeros(16000)]
            daemon.ouvido.transcrever_async = slow_stage
        else:
            daemon._limpar = slow_stage
        task = daemon._gap_task = asyncio.create_task(daemon._enviar_apos_pausa())
        await started.wait()
        await daemon._on_cmd({"type": "mute", "on": True})
        await daemon._on_cmd({"type": "mute", "on": False})
        release.set()
        await task
        assert not any(kind == "utterance" for kind, _ in emitted)
    asyncio.run(scenario())


@pytest.mark.parametrize("classification", [None, "abort", "skip"])
def test_pause_resume_discards_inflight_classification(classification):
    async def scenario():
        daemon, emitted = daemon_fixture()
        started, release = asyncio.Event(), asyncio.Event()

        async def classify(text):
            started.set()
            await release.wait()
            return classification

        daemon._intencao = classify
        task = asyncio.create_task(daemon._main_loop())
        daemon.events.put_nowait(("utterance", {"text": "Stale captured instruction"}))
        await started.wait()
        await daemon._on_cmd({"type": "mute", "on": True})
        await daemon._on_cmd({"type": "mute", "on": False})
        release.set()
        daemon.events.put_nowait(("quit", None))
        await task
        if daemon._gap_task:
            await daemon._gap_task
        assert not any(kind in {"utterance", "partial", "keyword", "abort"} for kind, _ in emitted)
    asyncio.run(scenario())


def test_fresh_speech_after_resume_reaches_final_turn():
    async def scenario():
        daemon, emitted = daemon_fixture()
        await daemon._on_cmd({"type": "mute", "on": True})
        await daemon._on_cmd({"type": "mute", "on": False})
        daemon._intencao = noop
        daemon.events.put_nowait(("utterance", {"text": "Fresh speech after resume."}))
        daemon.events.put_nowait(("quit", None))
        await daemon._main_loop()
        await daemon._gap_task
        assert ("utterance", {"text": "Fresh speech after resume."}) in emitted
    asyncio.run(scenario())


def test_muted_queued_speech_never_reaches_classifier():
    async def scenario():
        daemon, emitted = daemon_fixture()
        async def classify(text):
            pytest.fail("Paused speech reached the classifier")
        daemon._intencao = classify
        await daemon._on_cmd({"type": "mute", "on": True})
        daemon.events.put_nowait(("utterance", {"text": "Stop all work"}))
        daemon.events.put_nowait(("quit", None))
        await daemon._main_loop()
    asyncio.run(scenario())


def test_quit_releases_microphone_and_discards_inflight_cleanup():
    async def scenario():
        daemon, emitted = daemon_fixture()
        started, release = asyncio.Event(), asyncio.Event()
        stopped = []
        daemon.ouvido.stop = lambda: stopped.append(True)

        async def clean(text):
            started.set()
            await release.wait()
            return text

        daemon._limpar = clean
        daemon._acumulado = "Never deliver this after shutdown."
        task = daemon._gap_task = asyncio.create_task(daemon._enviar_apos_pausa())
        await started.wait()
        await daemon._on_cmd({"type": "quit"})
        assert stopped, "Quit acknowledged before releasing the microphone"
        release.set()
        await task
        assert not any(kind == "utterance" for kind, _ in emitted)
    asyncio.run(scenario())


def test_pause_resume_during_playback_stop_discards_pending_speech():
    async def scenario():
        daemon, emitted = daemon_fixture()
        started, release = asyncio.Event(), asyncio.Event()
        daemon._intencao = noop

        async def stop():
            started.set()
            await release.wait()

        daemon.fala.stop = stop
        task = asyncio.create_task(daemon._main_loop())
        daemon.events.put_nowait(("utterance", {"text": "Old pending speech"}))
        await started.wait()
        daemon.fala.stop = noop
        await daemon._on_cmd({"type": "mute", "on": True})
        await daemon._on_cmd({"type": "mute", "on": False})
        release.set()
        daemon.events.put_nowait(("quit", None))
        await task
        assert not any(kind in {"partial", "utterance"} for kind, _ in emitted)
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["tts", "stt"])
def test_quit_during_warmup_closes_microphone_without_announcing_ready(monkeypatch, stage):
    async def scenario():
        daemon, emitted = daemon_fixture()
        daemon.cfg["tts"]["backend"] = "mlx"
        started, release = asyncio.Event(), threading.Event()
        stopped, closed = [], []
        loop = asyncio.get_running_loop()

        def warmup():
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5), "Test did not release warmup"

        ear = SimpleNamespace(start=lambda: None, warmup=warmup if stage == "stt" else lambda: None, stop=lambda: stopped.append(True))
        voice = SimpleNamespace(start=lambda: None, stop=noop, close=noop, _mlx_pool=None, _carregar_mlx=warmup if stage == "tts" else lambda: None)
        async def close_ui():
            closed.append(True)
        ui = SimpleNamespace(start=noop, close=close_ui)
        module = importlib.import_module("voz.daemon")
        monkeypatch.setattr(module, "Ouvido", lambda *args: ear)
        monkeypatch.setattr(module, "Fala", lambda *args, **kwargs: voice)
        monkeypatch.setattr(module, "UI", lambda *args: ui)
        task = asyncio.create_task(daemon.run())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await daemon._on_cmd({"type": "quit"})
            assert stopped == [True], "Microphone stayed open while warmup completed"
        finally:
            release.set()
            await task
        assert stopped == [True], "Microphone cleanup must be idempotent"
        assert closed
        assert not any(kind == "ready" for kind, _ in emitted)
    asyncio.run(scenario())


def test_quit_rejects_new_speech_while_process_is_exiting():
    async def scenario():
        daemon, _ = daemon_fixture()
        said = []
        daemon.fala.dizer = said.append
        await daemon._on_cmd({'type': 'quit'})
        reply = await daemon._on_cmd({'type': 'say', 'text': 'This must not play after off.'})
        assert said == []
        assert reply['ok'] is False
    asyncio.run(scenario())


def test_delayed_first_transcript_cannot_send_while_continuation_is_recording_or_transcribing():
    async def scenario():
        daemon, emitted = daemon_fixture()
        daemon.cfg["vad"]["turn_gap_ms"] = 10
        daemon._intencao = noop
        task = asyncio.create_task(daemon._main_loop())
        try:
            for event in [("speech_start", None), ("rec_end", .5),
                          ("speech_start", None), ("utterance", {"text": "Add."})]:
                daemon.events.put_nowait(event)
            await asyncio.sleep(.03)
            assert not any(k == "utterance" for k, _ in emitted), "Sent the first word while the user continued speaking"
            daemon.events.put_nowait(("rec_end", 2.0))
            await asyncio.sleep(.03)
            assert not any(k == "utterance" for k, _ in emitted), "Sent before the continuation finished transcribing"
            daemon.events.put_nowait(("utterance", {"text": "Three more voice slots. I'm going to give you three more files."}))
            await asyncio.sleep(.03)
            delivered = [p["text"] for k, p in emitted if k == "utterance"]
            assert delivered == ["Add. Three more voice slots. I'm going to give you three more files."]
        finally:
            daemon.events.put_nowait(("quit", None))
            await task
            if daemon._gap_task:
                daemon._gap_task.cancel()
    asyncio.run(scenario())


def test_continuation_during_final_cleanup_keeps_the_original_words():
    async def scenario():
        daemon, emitted = daemon_fixture()
        entered, release = asyncio.Event(), asyncio.Event()
        first = True

        async def clean(text):
            nonlocal first
            if first:
                first = False
                entered.set()
                await release.wait()
            return text

        daemon._limpar = clean
        daemon._intencao = noop
        daemon._acumulado = "Add."
        original = daemon._gap_task = asyncio.create_task(daemon._enviar_apos_pausa())
        await entered.wait()
        task = asyncio.create_task(daemon._main_loop())
        daemon.events.put_nowait(("speech_start", None))
        await asyncio.sleep(0)
        release.set()
        daemon.events.put_nowait(("rec_end", 1.0))
        daemon.events.put_nowait(("utterance", {"text": "Three more files."}))
        daemon.events.put_nowait(("quit", None))
        await task
        await original
        if daemon._gap_task:
            await daemon._gap_task
        assert [p["text"] for k, p in emitted if k == "utterance"] == ["Add. Three more files."]
    asyncio.run(scenario())


def test_discarded_continuation_does_not_strand_prior_valid_words():
    async def scenario():
        daemon, emitted = daemon_fixture()
        daemon._acumulado = "A complete valid question."
        daemon._gap_task = asyncio.create_task(daemon._enviar_apos_pausa())
        daemon.events.put_nowait(("speech_start", None))
        daemon.events.put_nowait(("rec_end", .5))
        daemon.events.put_nowait(("speech_discarded", "Noise"))
        daemon.events.put_nowait(("quit", None))
        await daemon._main_loop()
        if daemon._gap_task:
            await daemon._gap_task
        assert [p["text"] for k, p in emitted if k == "utterance"] == ["A complete valid question."]
    asyncio.run(scenario())

"""Pause must invalidate buffered audio and recognition already in flight."""
import asyncio
import pytest
from types import SimpleNamespace

import numpy as np

from voz.config import load_config
from voz.listener import Ouvido


def test_speaker_mode_discards_tts_and_tail_audio_before_recognition(monkeypatch):
    async def scenario():
        clock = [10.0]
        monkeypatch.setattr("voz.listener.time.monotonic", lambda: clock[0])
        cfg = load_config()
        cfg["vad"]["barge_in"] = False
        queue = asyncio.Queue()
        ear = Ouvido(cfg, asyncio.get_running_loop(), queue)
        ear._on_audio(b"old pre-roll", 0, None, None)
        queue.put_nowait(("utterance", {"text": "old queued transcription"}))
        queue.put_nowait(("quit", None))
        ear.speaking_out = True
        ear._on_audio(b"assistant speech", 0, None, None)
        ear.speaking_out = False
        clock[0] += 0.49
        ear._on_audio(b"speaker echo tail", 0, None, None)
        assert ear._next_chunk() is None, "Speaker audio survived into recognition input"
        assert queue.get_nowait() == ("quit", None)
        assert queue.empty()
        clock[0] += 0.02
        fresh = np.full(48000, 1000, dtype=np.int16).tobytes()
        ear._on_audio(fresh, 0, None, None)
        ear.transcrever = lambda audio: {"text": "Fresh user speech after cooldown.", "segments": []}
        ear._transcribe(ear._next_chunk())
        await asyncio.sleep(0)
        assert queue.get_nowait()[1]["text"] == "Fresh user speech after cooldown."
        ear.stop()
    asyncio.run(scenario())


def test_tts_start_invalidates_recognition_that_finishes_after_cooldown(monkeypatch):
    async def scenario():
        clock = [10.0]
        monkeypatch.setattr("voz.listener.time.monotonic", lambda: clock[0])
        cfg = load_config()
        cfg["vad"]["barge_in"] = False
        queue = asyncio.Queue()
        ear = Ouvido(cfg, asyncio.get_running_loop(), queue)
        def recognize(audio):
            ear.speaking_out = True
            ear.speaking_out = False
            clock[0] += 0.6
            return {"text": "Old speech must never route.", "segments": []}
        ear.transcrever = recognize
        ear._transcribe(np.zeros(48000, dtype=np.int16).tobytes())
        await asyncio.sleep(0)
        assert queue.empty(), "Old recognition survived self-speech and cooldown"
        ear.stop()
    asyncio.run(scenario())


def test_headset_mode_keeps_capture_and_recognition_during_tts():
    async def scenario():
        cfg = load_config()
        cfg["vad"]["barge_in"] = True
        queue = asyncio.Queue()
        ear = Ouvido(cfg, asyncio.get_running_loop(), queue)
        ear.speaking_out = True
        raw = np.zeros(48000, dtype=np.int16).tobytes()
        ear._on_audio(raw, 0, None, None)
        ear.transcrever = lambda audio: {"text": "Please interrupt this response.", "segments": []}
        ear._transcribe(ear._next_chunk())
        await asyncio.sleep(0)
        assert queue.get_nowait()[1]["text"] == "Please interrupt this response."
        ear.stop()
    asyncio.run(scenario())


def test_speaker_playback_clears_vad_preroll_before_next_utterance(monkeypatch):
    async def scenario():
        clock = [10.0]
        monkeypatch.setattr("voz.listener.time.monotonic", lambda: clock[0])
        cfg = load_config()
        cfg["vad"].update(barge_in=False, mode="ptt")
        cfg["stt"]["min_duration_s"] = 0
        queue = asyncio.Queue()
        ear = Ouvido(cfg, asyncio.get_running_loop(), queue)
        ear._stt.shutdown()
        ear._stt = SimpleNamespace(submit=lambda fn, *args: fn(*args), shutdown=lambda **kwargs: None)
        recognized = []
        def recognize(audio):
            recognized.append(audio)
            return {"text": "Fresh speech.", "segments": []}
        ear.transcrever = recognize

        def chunks():
            yield np.full(ear.chunk, 1000, dtype=np.int16).tobytes()  # old pre-roll
            ear.speaking_out = True
            yield np.full(ear.chunk, 2000, dtype=np.int16).tobytes()  # self-speech
            ear.speaking_out = False
            clock[0] += 0.6
            yield np.full(ear.chunk, 3000, dtype=np.int16).tobytes()
            yield np.full(ear.chunk, 3000, dtype=np.int16).tobytes()  # fresh pre-roll
            ear.ptt(True)
            yield np.full(ear.chunk, 4000, dtype=np.int16).tobytes()
            ear.ptt(False)
            yield np.zeros(ear.chunk, dtype=np.int16).tobytes()

        source = chunks()
        ear._next_chunk = lambda: next(source)
        try:
            ear._run()
        except StopIteration:
            pass
        await asyncio.sleep(0)
        assert len(recognized) == 1
        assert set(np.unique(recognized[0])) == {3000 / 32768, 4000 / 32768}
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert any(kind == "utterance" and payload["text"] == "Fresh speech."
                   for kind, payload in events)
        ear.stop()
    asyncio.run(scenario())


def test_pause_resume_discards_inflight_recognition():
    async def scenario():
        queue = asyncio.Queue()
        ear = Ouvido(load_config(), asyncio.get_running_loop(), queue)
        def recognize(audio):
            ear.set_muted(True)
            ear.set_muted(False)
            return {"text": "This sentence must never reach Claude.", "segments": []}
        ear.transcrever = recognize
        ear._transcribe(np.zeros(48000, dtype=np.int16).tobytes())
        await asyncio.sleep(0)
        ear.stop()
        assert queue.empty(), "Recognition from before pause leaked after resume"
    asyncio.run(scenario())


def test_pause_discards_buffered_audio_and_queued_events():
    async def scenario():
        queue = asyncio.Queue()
        ear = Ouvido(load_config(), asyncio.get_running_loop(), queue)
        ear._on_audio(b'old audio', 0, None, None)
        queue.put_nowait(('utterance', {'text': 'stale queued words'}))
        queue.put_nowait(('quit', None))
        ear.set_muted(True)
        ear._on_audio(b'paused audio', 0, None, None)
        ear.set_muted(False)
        assert ear._next_chunk() is None, "Audio captured before or during pause survived"
        assert queue.get_nowait() == ('quit', None)
        assert queue.empty()
        ear.stop()
    asyncio.run(scenario())


def test_fresh_recognition_after_resume_is_delivered():
    async def scenario():
        queue = asyncio.Queue()
        ear = Ouvido(load_config(), asyncio.get_running_loop(), queue)
        ear.set_muted(True)
        ear.set_muted(False)
        ear.transcrever = lambda audio: {'text': 'Fresh words after resume.', 'segments': []}
        ear._transcribe(np.zeros(48000, dtype=np.int16).tobytes())
        await asyncio.sleep(0)
        assert queue.get_nowait()[1]['text'] == 'Fresh words after resume.'
        ear.stop()
    asyncio.run(scenario())


def test_short_english_question_reaches_utterance_boundary():
    async def scenario():
        queue = asyncio.Queue()
        ear = Ouvido(load_config(), asyncio.get_running_loop(), queue)
        question = "Can you give me more details?"
        ear.transcrever = lambda audio: {
            "text": question,
            "segments": [{"avg_logprob": -0.2, "no_speech_prob": 0.01}],
        }
        ear._transcribe(np.zeros(int(ear.sr * 1.6), dtype=np.int16).tobytes())
        await asyncio.sleep(0)
        event = queue.get_nowait()
        ear.stop()
        assert event[0] == "utterance", event
        assert event[1]["text"] == question
    asyncio.run(scenario())


def test_hallucination_guards_remain_active_with_english_reasons():
    cases = [
        ("", [], 2.0, "Empty transcript"),
        ("Can you give me more details?", [{"no_speech_prob": .9}], 1.6, "No speech detected"),
        ("I'm sorry.", [{"avg_logprob": -1.44}], 1.6, "Low confidence (-1.44)"),
        ("Repeated words here.", [{"compression_ratio": 2.5}], 2.0, "Repetitive transcript"),
        ("Thank you.", [], 2.0, "Likely hallucination"),
        ("Can you give me more details?", [], .5, "Implausible speech rate"),
    ]
    for text, segments, duration, reason in cases:
        assert Ouvido._suspeito(text, segments, duration) == reason


def test_failed_recognition_from_before_pause_cannot_reset_new_capture():
    async def scenario():
        queue = asyncio.Queue()
        ear = Ouvido(load_config(), asyncio.get_running_loop(), queue)
        def recognize(audio):
            ear.set_muted(True)
            ear.set_muted(False)
            raise RuntimeError("old recognition failed")
        ear.transcrever = recognize
        ear._transcribe(np.zeros(48000, dtype=np.int16).tobytes())
        await asyncio.sleep(0)
        try:
            assert queue.empty(), "Stale failure could clear the next capture's Hearing state"
        finally:
            ear.stop()
    asyncio.run(scenario())


def test_late_live_partial_cannot_reappear_after_capture_closes(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "mlx.core", SimpleNamespace(array=lambda a: a))
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=sys.modules["mlx.core"]))

    async def scenario(worker_finishes_first, close_capture=True):
        queue = asyncio.Queue()
        ear = Ouvido(load_config(), asyncio.get_running_loop(), queue)
        ear._live.shutdown()
        work = []
        ear._live = SimpleNamespace(submit=lambda fn, *args: work.append(lambda: fn(*args)), shutdown=lambda **kwargs: None)
        ear._parakeet = object()
        ear._live_stream = SimpleNamespace(add_audio=lambda a: None, result=SimpleNamespace(text="Old Hearing text"), __exit__=lambda *a: None)
        ear._live_feed([np.zeros(512, dtype=np.int16).tobytes()])
        if worker_finishes_first:
            work.pop(0)()  # partial queued on event loop, not delivered yet
        if close_capture:
            ear._live_close()
        while work:
            work.pop(0)()
        await asyncio.sleep(0)
        try:
            if close_capture:
                assert queue.empty(), "Closed capture emitted a late Hearing partial"
            else:
                assert queue.get_nowait() == ("live", "Old Hearing text")
        finally:
            ear.stop()
    asyncio.run(scenario(False))
    asyncio.run(scenario(True))
    asyncio.run(scenario(False, close_capture=False))


def _drive_auto_vad(ear, frames):
    """Run the detection loop over (amplitude, vad_probability) frames; return emitted event kinds."""
    events = []
    ear._emit = lambda kind, payload, generation=None, **kw: events.append(kind)
    ear._live_start = ear._live_close = lambda *a, **kw: None
    ear._live_feed = lambda *a, **kw: None
    probs = iter(p for _, p in frames)
    ear.vad = lambda chunk: next(probs)
    source = (np.full(ear.chunk, amp, dtype=np.int16).tobytes() for amp, _ in frames)
    ear._next_chunk = lambda: next(source)
    try:
        ear._run()
    except StopIteration:
        pass
    return events


def test_breathing_in_a_quiet_room_never_starts_recording():
    async def scenario():
        cfg = load_config()
        cfg["vad"].update(mode="auto", start_threshold=0.6, end_threshold=0.4, min_speech_ms=250, noise_margin_db=10.0)
        ear = Ouvido(cfg, asyncio.get_running_loop(), asyncio.Queue())
        room = [(30, 0.05)] * 40     # quiet room, about -61 dBFS
        breath = [(60, 0.9)] * 30     # speech-like to the VAD, but only 6 dB above the room
        events = _drive_auto_vad(ear, room + breath)
        ear.stop()
        assert "speech_start" not in events
    asyncio.run(scenario())


def test_clear_speech_above_the_room_level_starts_recording():
    async def scenario():
        cfg = load_config()
        cfg["vad"].update(mode="auto", start_threshold=0.6, end_threshold=0.4, min_speech_ms=250, noise_margin_db=10.0)
        ear = Ouvido(cfg, asyncio.get_running_loop(), asyncio.Queue())
        room = [(30, 0.05)] * 40
        speech = [(3000, 0.95)] * 12  # about -21 dBFS for 384 ms
        events = _drive_auto_vad(ear, room + speech)
        ear.stop()
        assert events.count("speech_start") == 1
    asyncio.run(scenario())


def test_short_speech_burst_below_the_confirm_time_is_ignored():
    async def scenario():
        cfg = load_config()
        cfg["vad"].update(mode="auto", start_threshold=0.6, end_threshold=0.4, min_speech_ms=250, noise_margin_db=10.0)
        ear = Ouvido(cfg, asyncio.get_running_loop(), asyncio.Queue())
        room = [(30, 0.05)] * 40
        cough = [(3000, 0.95)] * 4 + [(30, 0.05)] * 10  # 128 ms, shorter than the 250 ms confirm time
        events = _drive_auto_vad(ear, room + cough)
        ear.stop()
        assert "speech_start" not in events
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["No, I seem to be doing much better. Thank you.", "Okay, bye for now, talk later.",
                                  "Thanks, that works."])
def test_stock_phrases_inside_real_sentences_are_kept(text):
    assert Ouvido._suspeito(text, [], 3.0) is None


@pytest.mark.parametrize("text", ["Thank you.", "Bye.", "Thanks for watching!", "you", "Subtitles by amara.org"])
def test_whole_utterance_stock_phrases_are_discarded(text):
    assert Ouvido._suspeito(text, [], 1.0) == "Likely hallucination"

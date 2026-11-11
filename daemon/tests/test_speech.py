"""English neural speech requests use Qwen's full language names."""
import asyncio
from types import SimpleNamespace

import sounddevice as sd
import pytest

from voz.config import load_config
from voz.speech import Fala


def test_english_neural_speech_uses_english_language():
    voice = Fala(load_config())
    seen = {}
    class Model:
        def generate(self, **kwargs):
            seen.update(kwargs)
            return iter(())
    voice._mlx = Model()
    voice._ref_audio = None
    list(voice._gerar_mlx('The voice assistant is ready.'))
    assert seen['lang_code'] == 'english'
    assert seen['text'] == 'The voice assistant is ready.'


@pytest.mark.parametrize("control", ["stop", "pause_resume", "skip"])
def test_control_discards_playback_waiting_for_executor(monkeypatch, control):
    async def scenario():
        voice = Fala(load_config())
        loop = asyncio.get_running_loop()
        scheduled = []
        played = []

        def defer(pool, fn, *args):
            future = loop.create_future()
            scheduled.append((fn, args, future))
            return future

        monkeypatch.setattr(loop, "run_in_executor", defer)
        monkeypatch.setattr(sd, "play", lambda *args, **kwargs: played.append(True))
        monkeypatch.setattr(sd, "wait", lambda: None)
        monkeypatch.setattr(sd, "stop", lambda: None)
        task = asyncio.create_task(voice._play_audio(([0.2], 24000)))
        await asyncio.sleep(0)
        assert scheduled
        if control == "pause_resume":
            await voice.pause()
            voice.resume()
        else:
            await getattr(voice, control)()
        fn, args, future = scheduled[0]
        future.set_result(fn(*args))
        await task
        assert not played, "Old audio started after the control completed"
        await voice.close()
    asyncio.run(scenario())


def test_stop_kills_say_process_created_after_stop(monkeypatch):
    async def scenario():
        voice = Fala(load_config())
        started, release = asyncio.Event(), asyncio.Event()
        killed = []

        async def wait():
            return 0

        process = SimpleNamespace(returncode=None, kill=lambda: killed.append(True), wait=wait)
        async def spawn(*args, **kwargs):
            started.set()
            await release.wait()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(sd, "stop", lambda: None)
        task = asyncio.create_task(voice._speak_say("Old speech"))
        await started.wait()
        await voice.stop()
        release.set()
        await task
        assert killed, "A late-created say process survived stop"
        await voice.close()
    asyncio.run(scenario())


def test_current_neural_audio_still_plays(monkeypatch):
    async def scenario():
        voice = Fala(load_config())
        played = []
        monkeypatch.setattr(sd, "play", lambda audio, sr, **kwargs: played.append((audio, sr)))
        monkeypatch.setattr(sd, "wait", lambda: None)
        monkeypatch.setattr(sd, "stop", lambda: None)
        await voice._play_audio(([0.2], 24000))
        assert played == [([0.2], 24000)]
        await voice.close()
    asyncio.run(scenario())


def test_current_say_speech_is_not_killed(monkeypatch):
    async def scenario():
        voice = Fala(load_config())
        spoken, killed = [], []
        async def wait():
            return 0
        process = SimpleNamespace(returncode=0, kill=lambda: killed.append(True), wait=wait)
        async def spawn(*args, **kwargs):
            spoken.append(args[-1])
            return process
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(sd, "stop", lambda: None)
        await voice._speak_say("Fresh spoken response.")
        assert spoken == ["Fresh spoken response."]
        assert not killed
        await voice.close()
    asyncio.run(scenario())


def test_native_voice_playback_uses_same_echo_cancelling_device(monkeypatch):
    import soundfile as sf
    async def scenario():
        voice = Fala(load_config())
        native_played, native_stopped, portaudio = [], [], []
        def play_file(path, rate=1.0):
            samples, sr = sf.read(path)
            native_played.append((len(samples), sr))
            future = asyncio.get_running_loop().create_future()
            future.set_result(None)
            return future
        voice.audio_device = SimpleNamespace(play_file=play_file, stop_playback=lambda: native_stopped.append(True))
        monkeypatch.setattr(sd, 'play', lambda *args, **kwargs: portaudio.append(True))
        monkeypatch.setattr(sd, 'wait', lambda: None)
        monkeypatch.setattr(sd, 'stop', lambda: None)
        await voice._play_audio(([0.1]*240, 24000))
        await voice.stop()
        assert native_played == [(240, 24000)]
        assert native_stopped
        assert portaudio == []
        await voice.close()
    asyncio.run(scenario())


def test_native_false_interruption_resumes_without_replaying_sentence():
    async def scenario():
        voice = Fala(load_config())
        voice.cfg['backend'] = 'mlx'
        voice._mlx = object()
        voice._gerar_audio_mlx = lambda text: text
        calls = []
        started = asyncio.Event()
        finished = asyncio.Event()
        voice.audio_device = SimpleNamespace(
            pause_playback=lambda: calls.append('pause'),
            resume_playback=lambda: calls.append('resume'),
            stop_playback=lambda: calls.append('stop'),
        )
        async def speak(text, gen):
            calls.append(text)
            started.set()
            await finished.wait()
        voice._play_audio = speak
        voice.start()
        voice.dizer('One continuous sentence.')
        await started.wait()
        await voice.pause()
        voice.resume()
        finished.set()
        await asyncio.wait_for(voice.esperar(), 1)
        assert calls == ['One continuous sentence.', 'pause', 'resume']
        await voice.close()
    asyncio.run(scenario())


def test_spoken_skip_while_paused_allows_next_sentence():
    async def scenario():
        voice = Fala(load_config())
        voice.cfg['backend'] = 'mlx'
        voice._mlx = object()
        voice._gerar_audio_mlx = lambda text: text
        spoken = []
        started = asyncio.Event()
        playback = asyncio.get_running_loop().create_future()
        def stop_playback():
            if not playback.done():
                playback.set_result(None)
        voice.audio_device = SimpleNamespace(pause_playback=lambda: None, stop_playback=stop_playback)
        async def speak(text, gen):
            spoken.append(text)
            if len(spoken) == 1:
                started.set()
                await playback
        voice._play_audio = speak
        voice.start()
        voice.dizer('Skip this sentence.')
        voice.dizer('This next sentence must play.')
        await started.wait()
        await voice.pause()
        await voice.skip()
        await asyncio.wait_for(voice.esperar(), 0.2)
        assert spoken == ['Skip this sentence.', 'This next sentence must play.']
        await voice.close()
    asyncio.run(scenario())


def test_voice_stays_busy_while_next_sentence_is_synthesized(monkeypatch):
    """A noise in the gap between two sentences must pause the reply, not end it."""
    async def scenario():
        states = []
        cfg = load_config()
        cfg["tts"]["backend"] = "mlx"
        voice = Fala(cfg, on_state=states.append)
        voice._play_q = asyncio.Queue(maxsize=3)
        loop = asyncio.get_running_loop()
        pending = []

        def defer(pool, fn, *args):
            future = loop.create_future()
            pending.append(future)
            return future

        monkeypatch.setattr(loop, "run_in_executor", defer)
        producer = asyncio.create_task(voice._producer())
        voice.dizer("The second sentence is still being generated.")
        await asyncio.sleep(0)
        assert pending, "synthesis did not start"
        assert voice.queue.empty() and voice._play_q.empty()
        assert voice.busy, "voice reported idle while a sentence was being synthesized"
        assert False not in states
        pending[0].set_result(None)
        await asyncio.sleep(0)
        assert not voice.busy
        assert states[-1] is False
        producer.cancel()
    asyncio.run(scenario())


def test_voice_stays_busy_while_a_sentence_plays_and_the_next_has_no_audio(monkeypatch):
    """The last sentence can give no audio while the one before it still plays. The voice must not report idle."""
    async def scenario():
        states = []
        cfg = load_config()
        cfg["tts"]["backend"] = "mlx"
        voice = Fala(cfg, on_state=states.append)
        voice._play_q = asyncio.Queue(maxsize=3)
        loop = asyncio.get_running_loop()
        pending = []

        def defer(pool, fn, *args):
            future = loop.create_future()
            pending.append(future)
            return future

        monkeypatch.setattr(loop, "run_in_executor", defer)
        producer = asyncio.create_task(voice._producer())
        voice._playing = True  # the consumer plays the sentence before this one
        voice.dizer("...")
        await asyncio.sleep(0)
        assert pending, "synthesis did not start"
        pending[0].set_result(None)  # this sentence gives no audio
        await asyncio.sleep(0)
        assert voice.busy, "voice reported idle while a sentence was still playing"
        assert False not in states
        voice._playing = False
        voice._maybe_idle()
        assert not voice.busy
        assert states[-1] is False
        producer.cancel()
    asyncio.run(scenario())

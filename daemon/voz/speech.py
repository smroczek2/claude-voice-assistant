"""Speech: sentence queue to local TTS, with immediate stop (barge-in).

Local neural backends: Chatterbox Turbo, Qwen3-TTS, and Kokoro via MLX.
Playback shares the native echo-cancelled microphone graph.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("voz.speech")

def _load_neural_model(repository: str):
    from mlx_audio.tts.utils import load_model
    if repository == 'mlx-community/Kokoro-82M-bf16':
        # A wildcard safetensors download also fetches every unwanted voice pack.
        return load_model(repository, allow_patterns=[
            'config.json', 'kokoro-v1_0.safetensors', 'voices/bm_george.safetensors',
        ])
    return load_model(repository)


def vozes_say() -> list[str]:
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return []
    nomes = []
    for line in out.splitlines():
        # format: "Joana (Premium)     pt_PT    # Olá..."
        nome = line.split("  ")[0].strip()
        if nome:
            nomes.append(nome)
    return nomes


class Fala:
    def __init__(self, cfg: dict[str, Any], on_state: Callable[[bool], None] | None = None, on_error: Callable[[str], None] | None = None):
        self.cfg = cfg["tts"]
        self.on_state = on_state or (lambda speaking: None)
        self.on_error = on_error or (lambda error: log.error('Speech: %s', error))
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._gen = 0  # generation: sentences from older generations are discarded after stop()
        self._playback_generation = 0
        self._playback_lock = threading.Lock()
        self._idle = asyncio.Event(); self._idle.set()
        self._synthesizing = 0  # sentences taken from the queue whose audio is still being generated
        self._playing = False  # a sentence is playing now (taken from the play queue)
        self._paused = False
        self._skip = False
        self._resume_evt = asyncio.Event(); self._resume_evt.set()
        self.voice = str(self.cfg["voice"])
        self._mlx = None
        self._kokoro = None
        self._turbo = None
        self._selection_lock = asyncio.Lock()
        self._mlx_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
        self.audio_device = None  # shared native AEC capture/playback when configured

    def start(self) -> None:
        # pipeline: the sentence queue feeds a producer (generates audio ahead) and a player (plays in order)
        self._play_q: asyncio.Queue = asyncio.Queue(maxsize=3)
        if self.cfg['backend'] not in {'mlx', 'kokoro', 'turbo'}:
            raise ValueError('Choose a local neural speech backend: mlx, kokoro, or turbo.')
        if self.cfg["backend"] == "mlx":
            try:
                if self._mlx is None:
                    self._carregar_mlx()
            except Exception as e:
                raise RuntimeError(f'Local neural speech could not start: {e}') from e
        elif self.cfg['backend'] == 'kokoro':
            if self._kokoro is None:
                self._carregar_kokoro()
        elif self.cfg['backend'] == 'turbo' and self._turbo is None:
            self._carregar_turbo()
        self._task = asyncio.create_task(self._producer(), name="fala-gera")
        self._player = asyncio.create_task(self._consumer(), name="fala-toca")

    # ---------- live voice selection ----------
    def refs(self) -> list[Path]:
        refs = []
        gravada = Path.home() / ".voz" / "ref.wav"
        if gravada.is_file():
            refs.append(gravada)
        private_dir = Path.home() / ".voz" / "refs"
        if private_dir.is_dir():
            refs += sorted(private_dir.glob("*.wav"))
        ref_dir = Path(__file__).resolve().parent.parent / "ref"
        if ref_dir.is_dir():
            refs += sorted(ref_dir.glob("*.wav"))
        return refs

    def vozes(self, lang: str | None = None) -> list[dict]:
        """Voices for the current (or given) language, installed ones first."""
        from .voices import procurar

        return procurar(lang or self.cfg.get("lang", "pt_PT"), None, self.refs())

    def _melhor_say(self, nome: str) -> str:
        """With prefer_premium, "Joana" becomes "Joana (Premium)" or "Joana (Enhanced)" if installed."""
        if not self.cfg.get("prefer_premium", True) or "(" in nome:
            return nome
        instaladas = vozes_say()
        for suf in (" (Premium)", " (Enhanced)"):
            if nome + suf in instaladas:
                return nome + suf
        return nome

    def voz_actual(self) -> str:
        b = self.cfg["backend"]
        if b == "mlx":
            return f"mlx:ref:{Path(self.cfg['mlx_ref_audio']).expanduser()}" if self.cfg.get("mlx_ref_audio") else f"mlx:voice:{self.cfg['mlx_voice']}"
        if b == 'kokoro':
            return f"kokoro:{self.cfg['kokoro_voice']}"
        if b == 'turbo':
            return 'turbo:default'
        if b == "piper":
            return f"piper:{Path(str(self.cfg['voice'])).stem}"
        return f"say:{self.voice}"

    async def mudar_voz(self, voz_id: str) -> str:
        """Select and prepare a supported local neural voice."""
        async with self._selection_lock:
            return await self._change_voice(voz_id)

    async def _change_voice(self, voz_id: str) -> str:
        selection_keys = ('backend', 'mlx_ref_audio', 'mlx_ref_text', 'mlx_voice', 'kokoro_voice')
        previous = {key: self.cfg[key] for key in selection_keys if key in self.cfg}
        previous_voice = self.voice
        runtime_keys = ('_mlx', '_mlx_modelo', '_ref_audio', '_ref_text', '_kokoro', '_turbo')
        previous_runtime = {key: getattr(self, key) for key in runtime_keys if hasattr(self, key)}
        try:
            return await self._select_voice(voz_id)
        except Exception:
            # Speed/expression may have changed while the model was loading.
            # Only undo this selection's own fields.
            for key in selection_keys:
                if key in previous:
                    self.cfg[key] = previous[key]
                else:
                    self.cfg.pop(key, None)
            self.voice = previous_voice
            for key in runtime_keys:
                if key in previous_runtime:
                    setattr(self, key, previous_runtime[key])
                elif hasattr(self, key):
                    delattr(self, key)
            raise

    async def _select_voice(self, voz_id: str) -> str:

        kind, _, rest = voz_id.partition(":")
        if kind not in {'mlx', 'kokoro', 'turbo'}:
            raise ValueError('Choose a local neural voice: Chatterbox Turbo, Qwen3, or Kokoro.')
        if kind == 'turbo':
            if rest != 'default':
                raise ValueError('Unknown Chatterbox Turbo voice')
            await self.stop()
            self.cfg['backend'] = 'turbo'
            await asyncio.get_running_loop().run_in_executor(self._mlx_pool, self._carregar_turbo)
            return 'Chatterbox Turbo'
        if kind == 'kokoro':
            from .voices import KOKORO_PRESETS
            if rest not in {v[0] for v in KOKORO_PRESETS}:
                raise ValueError('Unknown English Kokoro neural voice')
            await self.stop()
            self.cfg['backend'] = 'kokoro'
            self.cfg['kokoro_voice'] = rest
            await asyncio.get_running_loop().run_in_executor(self._mlx_pool, self._carregar_kokoro)
            return f'kokoro/{self.voice}'
        sub, _, valor = rest.partition(":")
        from .voices import QWEN_PRESETS
        if not valor or sub not in {'ref', 'voice'} or (sub == 'voice' and valor not in QWEN_PRESETS):
            raise ValueError('Choose a supported local neural voice.')
        await self.stop()
        self.cfg["backend"] = "mlx"
        if sub == "ref":
            self.cfg["mlx_ref_audio"] = valor
            self.cfg["mlx_ref_text"] = ""
        else:
            self.cfg["mlx_ref_audio"] = ""
            self.cfg["mlx_voice"] = valor
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._mlx_pool, self._carregar_mlx)
        return f"mlx/{self.voice}"

    def _carregar_kokoro(self) -> None:
        if self._kokoro is None:
            self._kokoro = _load_neural_model(self.cfg['kokoro_model'])
        self.voice = self.cfg['kokoro_voice']
        # Warm English text processing and the selected voice without playing.
        list(self._gerar_kokoro('Hello.'))

    def _gerar_kokoro(self, frase: str):
        voice = self.cfg['kokoro_voice']
        return self._kokoro.generate(text=frase, voice=voice, lang_code='b' if voice.startswith('b') else 'a', speed=1.0)

    def _carregar_turbo(self) -> None:
        if self._turbo is None:
            candidate = _load_neural_model(self.cfg['turbo_model'])
            list(candidate.generate(text='Hello.', verbose=False, exaggeration=0.0, cfg_weight=0.0))
            self._turbo = candidate
        self.voice = 'Chatterbox Turbo'

    def _gerar_turbo(self, frase: str):
        # Use the bundled voice approved in the preview; no reference or cloning.
        return self._turbo.generate(text=frase, verbose=False, exaggeration=0.0, cfg_weight=0.0)

    def _carregar_mlx(self) -> None:
        """Load Qwen3-TTS. With `mlx_ref_audio` it clones that voice (Base model); otherwise it uses `mlx_voice` (CustomVoice).
        Runs a warm-up generation so the first real sentence is not slow."""
        import logging as _l

        _l.getLogger("transformers").setLevel(_l.ERROR)
        ref = self.cfg.get("mlx_ref_audio") or ""
        self._ref_audio = str(Path(ref).expanduser()) if ref else None
        self._ref_text = self.cfg.get("mlx_ref_text") or ""
        if self._ref_audio and not self._ref_text:
            txt = Path(self._ref_audio).with_suffix(".txt")
            if txt.is_file():
                self._ref_text = txt.read_text().strip()
        log.info("loading TTS %s%s…", self.cfg["mlx_model"], f" (clone of {Path(self._ref_audio).name})" if self._ref_audio else "")
        modelo = self.cfg['mlx_model'].replace('-CustomVoice-', '-Base-') if self._ref_audio else self.cfg['mlx_model'].replace('-Base-', '-CustomVoice-')
        previous_model = self._mlx
        candidate = self._mlx if getattr(self, '_mlx_modelo', None) == modelo else _load_neural_model(modelo)
        actual = getattr(getattr(candidate, 'config', None), 'tts_model_type', None)
        expected = 'base' if self._ref_audio else 'custom_voice'
        if actual is not None and actual != expected:
            raise ValueError(f'Qwen selection requires {expected}, but the loaded model is {actual}')
        self._mlx = candidate
        try:
            for _ in self._gerar_mlx("Hello." if self.cfg.get("lang") == "en" else "Olá."):
                break
        except Exception:
            self._mlx = previous_model
            raise
        self._mlx_modelo = modelo
        self.voice = Path(self._ref_audio).stem if self._ref_audio else str(self.cfg["mlx_voice"])

    def _gerar_mlx(self, frase: str):
        from .voices import familia

        languages = {"en": "english", "pt": "portuguese", "zh": "chinese", "ja": "japanese", "ko": "korean", "de": "german", "fr": "french", "ru": "russian", "es": "spanish", "it": "italian"}
        lang = languages.get(familia(self.cfg.get("lang", "en")), "auto")
        kw = dict(text=frase, lang_code=lang, verbose=False)
        if self._ref_audio:
            kw.update(ref_audio=self._ref_audio, ref_text=self._ref_text)
        else:
            kw.update(voice=self.cfg["mlx_voice"])
            if self.cfg.get('expression', 'restrained') == 'restrained':
                kw['instruct'] = 'A calm, matter-of-fact delivery with steady volume and minimal emotion.'
            elif self.cfg.get('expression') == 'conversational':
                kw['instruct'] = 'An attentive everyday conversation with clear articulation, light natural inflection and a steady speaking voice.'
        return self._mlx.generate(**kw)

    async def close(self) -> None:
        await self.stop()
        for t in (self._task, getattr(self, "_player", None)):
            if t:
                t.cancel()
        self._mlx_pool.shutdown(wait=False)

    def dizer(self, frase: str) -> None:
        frase = frase.strip()
        if frase:
            self._idle.clear()
            self.queue.put_nowait(f"{self._gen}\x00{frase}")

    async def esperar(self) -> None:
        await self._idle.wait()

    async def skip(self) -> None:
        """Skip the sentence being played and continue with the next one."""
        self._skip = True
        self._paused = False
        self._resume_evt.set()
        p = self._proc
        if p and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
        self._stop_audio()

    async def pause(self) -> None:
        """Pause native playback in place while recognition checks a possible interruption."""
        if self._paused:
            return
        self._paused = True
        self._resume_evt.clear()
        if self.audio_device is not None:
            self.audio_device.pause_playback()
            return
        p = self._proc
        if p and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
        self._stop_audio()

    def resume(self) -> None:
        """Resume native playback at its cursor; legacy backends repeat the sentence."""
        if not self._paused:
            return
        self._paused = False
        if self.audio_device is not None:
            self.audio_device.resume_playback()
        self._resume_evt.set()

    @property
    def busy(self) -> bool:
        """True while any sentence of the reply is queued, being synthesized, waiting to play, or playing."""
        play_q = getattr(self, "_play_q", None)
        return self._playing or self._synthesizing > 0 or not self.queue.empty() or (play_q is not None and not play_q.empty())

    def _maybe_idle(self) -> None:
        if not self.busy:
            self.on_state(False)
            self._idle.set()

    @property
    def paused(self) -> bool:
        return self._paused

    async def stop(self) -> None:
        """Stop now: kill the current process, stop the audio and discard the queues."""
        self._paused = False
        self._resume_evt.set()
        self._gen += 1
        self._skip = False
        for q in (self.queue, getattr(self, "_play_q", None)):
            while q is not None and not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
        p = self._proc
        if p and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
        self._stop_audio()
        self._idle.set()

    def _stop_audio(self) -> None:
        # The worker's generation check and sd.play must be atomic against stop.
        # Do not hold this lock during sd.wait: stop must interrupt active audio.
        with self._playback_lock:
            self._playback_generation += 1
            if self.audio_device is not None:
                self.audio_device.stop_playback()
                return
            try:
                import sounddevice as sd

                sd.stop()
            except Exception:  # noqa: BLE001
                pass

    # ---------- internal ----------
    async def _producer(self) -> None:
        """Convert sentences to audio ahead of the player (up to 2 waiting)."""
        while True:
            item = await self.queue.get()
            gen_s, frase = item.split("\x00", 1)
            gen = int(gen_s)
            if gen != self._gen:
                self._maybe_idle()
                continue
            self._synthesizing += 1
            try:
                if self.cfg["backend"] in {"mlx", "kokoro", "turbo"}:
                    audio = await asyncio.get_running_loop().run_in_executor(self._mlx_pool, self._gerar_audio_mlx, frase)
                    if gen != self._gen or audio is None:
                        continue
                    await self._play_q.put((gen, "audio", audio))
                elif self.cfg["backend"] == "piper":
                    audio = await asyncio.get_running_loop().run_in_executor(self._mlx_pool, self._gerar_audio_piper, frase)
                    if gen != self._gen or audio is None:
                        continue
                    await self._play_q.put((gen, "audio", audio))
                else:
                    await self._play_q.put((gen, "text", frase))
            except Exception as e:  # noqa: BLE001
                self.on_error(f'Local neural generation failed: {e}')
            finally:
                self._synthesizing -= 1
                self._maybe_idle()

    async def _consumer(self) -> None:
        """Play in order what the producer prepared."""
        while True:
            gen, kind, payload = await self._play_q.get()
            if gen != self._gen:
                continue
            self.on_state(True)
            self._playing = True
            try:
                while True:
                    await self._resume_evt.wait()
                    if gen != self._gen:
                        break
                    if kind == "audio":
                        await self._play_audio(payload, gen)
                    else:
                        await self._speak_say(payload, gen)
                    if self._skip:
                        self._skip = False
                        break  # skipped: move on to the next sentence
                    if self.audio_device is not None or not self._paused:
                        break  # played to the end; if paused midway, repeat after resume()
            except Exception as e:  # noqa: BLE001
                self.on_error(f'Local speech playback failed: {e}')
            finally:
                self._playing = False
                self._maybe_idle()

    def _gerar_audio_mlx(self, frase: str):
        """Generate audio and trim the silence at both ends. Returns (np.ndarray float32, sample_rate) or None."""
        import numpy as np

        chunks = []
        sr = None
        generate = {'kokoro': self._gerar_kokoro, 'mlx': self._gerar_mlx, 'turbo': self._gerar_turbo}[self.cfg['backend']]
        results = generate(frase)
        for r in results:
            if sr is not None and sr != int(r.sample_rate):
                raise RuntimeError('Neural segments changed sample rate')
            sr = int(r.sample_rate)
            chunks.append(np.asarray(r.audio, dtype=np.float32).reshape(-1))
        if chunks:
            a = np.concatenate(chunks)
            # trim the silence at both ends (the model leaves up to half a second) and shorten
            # long internal pauses (over 450 ms) to 220 ms so the full stop sounds natural
            loud = np.where(np.abs(a) > 0.008)[0]
            if len(loud):
                margem = int(sr * 0.04)
                a = a[max(0, loud[0] - margem): min(len(a), loud[-1] + margem)]
                a = self._encurtar_pausas(a, sr)
            return a, sr
        return None

    @staticmethod
    def _encurtar_pausas(a, sr: int, max_ms: int = 450, keep_ms: int = 220, thr: float = 0.008):
        """Reduce internal silences longer than max_ms to keep_ms, in 10 ms blocks."""
        import numpy as np

        hop = int(sr * 0.01)
        if len(a) < hop * 4:
            return a
        n = len(a) // hop
        rms = np.sqrt(np.mean(a[: n * hop].reshape(n, hop) ** 2, axis=1))
        quiet = rms < thr
        out = []
        i = 0
        max_n, keep_n = max_ms // 10, keep_ms // 10
        while i < n:
            if not quiet[i]:
                out.append(a[i * hop: (i + 1) * hop]); i += 1; continue
            j = i
            while j < n and quiet[j]:
                j += 1
            run = j - i
            if run > max_n:
                out.append(a[i * hop: (i + keep_n) * hop])
            else:
                out.append(a[i * hop: j * hop])
            i = j
        out.append(a[n * hop:])
        return np.concatenate(out) if out else a

    async def _play_audio(self, payload, generation: int | None = None) -> None:
        import sounddevice as sd

        audio, sr = payload
        generation = self._gen if generation is None else generation
        playback_generation = self._playback_generation
        if self.audio_device is not None:
            import soundfile as sf

            fd, path = tempfile.mkstemp(suffix='.wav', prefix='voz-aec-')
            os.close(fd)
            try:
                sf.write(path, audio, sr)
                with self._playback_lock:
                    if generation != self._gen or playback_generation != self._playback_generation or self._paused or self._skip:
                        return
                    played = self.audio_device.play_file(path, rate=self.speed)
                await played
            finally:
                Path(path).unlink(missing_ok=True)
            return

        def tocar() -> None:
            with self._playback_lock:
                if generation != self._gen or playback_generation != self._playback_generation or self._paused or self._skip:
                    return
                sd.play(audio, sr, blocking=False)
            sd.wait()

        await asyncio.get_running_loop().run_in_executor(None, tocar)

    @property
    def speed(self) -> float:
        return float(self.cfg.get("speed", 1.0))

    async def _speak_say(self, frase: str, generation: int | None = None) -> None:
        generation = self._gen if generation is None else generation
        playback_generation = self._playback_generation
        if generation != self._gen or self._paused or self._skip:
            return
        if self.audio_device is not None:
            # Synthesize Apple's fallback to a file, then render through AEC too.
            fd, path = tempfile.mkstemp(suffix='.aiff', prefix='voz-aec-say-')
            os.close(fd)
            try:
                self._proc = await asyncio.create_subprocess_exec('say', '-v', self.voice, '-r', str(int(float(self.cfg['rate']) * self.speed)), '-o', path, frase)
                if generation != self._gen or playback_generation != self._playback_generation:
                    self._proc.kill()
                await self._proc.wait()
                if generation == self._gen and playback_generation == self._playback_generation:
                    await self.audio_device.play_file(path)
            finally:
                Path(path).unlink(missing_ok=True)
            return
        saida = [] if not self.cfg.get("output_device") else ["-a", str(self.cfg["output_device"])]
        self._proc = await asyncio.create_subprocess_exec(
            "say", "-v", self.voice, "-r", str(int(float(self.cfg["rate"]) * self.speed)), *saida, frase,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        if generation != self._gen or playback_generation != self._playback_generation or self._paused or self._skip:
            if self._proc.returncode is None:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
        await self._proc.wait()

    async def _play(self, wav: str) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            "afplay", wav, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await self._proc.wait()

    async def _speak_mlx(self, frase: str, gen: int) -> None:
        import soundfile as sf

        def gerar() -> str | None:
            fd, wav = tempfile.mkstemp(suffix=".wav", prefix="voz-"); os.close(fd)
            for r in self._gerar_mlx(frase):
                sf.write(wav, r.audio, r.sample_rate)
                return wav
            return None

        wav = await asyncio.get_running_loop().run_in_executor(self._mlx_pool, gerar)
        if not wav:
            return
        try:
            if gen == self._gen:  # stop() may have been called during generation
                await self._play(wav)
        finally:
            try:
                os.unlink(wav)
            except OSError:
                pass

    def _gerar_audio_piper(self, frase: str):
        """Piper through the Python API (the package CLI has a broken espeak-ng path). Returns (float32, sr)."""
        import io
        import wave

        import numpy as np
        from piper import PiperVoice, SynthesisConfig

        modelo = str(self.cfg["voice"])
        if getattr(self, "_piper_modelo", None) != modelo:
            self._piper = PiperVoice.load(modelo)
            self._piper_modelo = modelo
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            self._piper.synthesize_wav(frase, w, syn_config=SynthesisConfig(length_scale=1.0 / max(0.5, self.speed)))
        buf.seek(0)
        with wave.open(buf, "rb") as w:
            sr = w.getframerate()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        return a, sr

    async def _speak_piper(self, frase: str) -> None:
        fd, wav = tempfile.mkstemp(suffix=".wav", prefix="voz-"); os.close(fd)
        try:
            from .voices import piper_bin

            self._proc = await asyncio.create_subprocess_exec(
                piper_bin(), "--model", str(self.cfg["voice"]), "--length_scale", f"{1.0 / self.speed:.2f}", "--output_file", wav,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await self._proc.communicate(frase.encode())
            if self._proc.returncode == 0:
                await self._play(wav)
        finally:
            try:
                os.unlink(wav)
            except OSError:
                pass


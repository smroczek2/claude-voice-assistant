"""Ouvido (listener): microphone → VAD (Silero) → Whisper (MLX).

Captures ONLY the configured input device (one microphone). Never system sound
or other applications: that would only get in if it played through the speakers and reached the microphone.

Runs on its own thread and delivers events to the asyncio loop:
  ("speech_start", None)      you started speaking (or pressed push-to-talk)
  ("rec_end", seconds)        the recording closed; about to transcribe
  ("utterance", text)         transcribed sentence
  ("speech_discarded", str)   recording discarded (short, noise, hallucination) and why
  ("level", float)            microphone level 0..1, ~10 times per second
"""

from __future__ import annotations

import asyncio
import collections
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import sounddevice as sd
from pysilero_vad import SileroVoiceActivityDetector

log = logging.getLogger("voz.listener")

# Phrases Whisper invents on silence or noise (it was trained on video subtitles). Portuguese patterns: support for Portuguese-speaking users.
# Stock phrases ("thank you", "bye") are hallucinations only when they ARE the whole
# utterance; inside a real sentence ("I'm doing better. Thank you.") they are speech.
# Subtitle credits are artifacts wherever they appear.
ALUCINACOES_RE = re.compile(
    r"(obrigad[oa] por (assistir|ver|ouvir)|legendas? (pela|por|da)|amara\.org|"
    r"thanks? for watching|please subscribe|inscreva-se|"
    r"^(thank you( very much)?|thanks|bye|bye-bye|goodbye|you|tchau|até logo|até (à|a) próxima|"
    r"boa noite a todos|obrigad[oa]|e|a|o|um|hum|\.{2,})$)",  # "sim", "não" and "ok" are legitimate answers, never hallucinations
    re.I,
)


class NoiseFloor:
    """Rolling room level (dBFS) from quiet frames. Speech must stand clearly above it.

    Apple's voice processing raises the gain in a quiet room, so breathing and faint
    background sound reach the VAD loud enough to look like speech. A level gate
    relative to the room, as in Pipecat's min_volume, rejects them.
    """

    MIN_SAMPLES = 15  # about 0.5 s of quiet before the measured floor replaces the initial guess

    def __init__(self, frames: int = 125, initial_db: float = -60.0):
        self._quiet: collections.deque = collections.deque(maxlen=frames)  # about 4 s of 32 ms frames
        self._initial = initial_db

    @staticmethod
    def level_db(chunk: bytes) -> float:
        a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(a * a))) if a.size else 0.0
        return 20.0 * math.log10(max(rms, 1.0) / 32768.0)

    def observe_quiet(self, db: float) -> None:
        self._quiet.append(db)

    def floor_db(self) -> float:
        if len(self._quiet) < self.MIN_SAMPLES:
            return self._initial
        return float(np.percentile(self._quiet, 20))


class Ouvido:
    def __init__(self, cfg: dict[str, Any], loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.cfg = cfg
        self.loop = loop
        self.queue = queue
        self.sr = int(cfg["audio"]["sample_rate"])
        self.vad = SileroVoiceActivityDetector()
        self.noise = NoiseFloor()
        self.chunk = SileroVoiceActivityDetector.chunk_samples()  # 512 samples at 16 kHz (32 ms)
        self.ptt_mode = cfg["vad"]["mode"] == "ptt"
        self._ptt_down = False
        self._stream: sd.InputStream | None = None
        self._raw: collections.deque = collections.deque()
        self._raw_lock = threading.Lock()
        self._stop = threading.Event()
        self._muted = False
        self.generation = 0  # invalidates audio and worker results across pause/resume
        self._speaking_out = False
        self._echo_until = 0.0
        self._stt = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        self._live = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live")
        self._parakeet = None          # Parakeet model (live text), loaded in warmup if stt.live
        self._live_stream = None
        self._live_pending = 0
        self._live_epoch = 0  # closes partial delivery independently of pause/resume
        self._thread: threading.Thread | None = None
        self.device_name = "?"
        self.native_audio = None
        if cfg["audio"].get("backend") == "native":
            from .native_audio import NativeAudio
            self.native_audio = NativeAudio(
                lambda raw: self._on_audio(raw, len(raw) // 2, None, None),
                lambda error: self.loop.call_soon_threadsafe(self.queue.put_nowait, ('fatal', error)),
                self._on_route,
            )

    def _on_route(self, info: dict) -> None:
        """Headphones or another device took over; the native graph already rebuilt itself."""
        self.device_name = str(info.get('mic') or self.device_name)
        log.info("microphone: %s (device changed)", self.device_name)

    # ---------- control ----------
    @property
    def speaking_out(self) -> bool:
        return self._speaking_out

    @speaking_out.setter
    def speaking_out(self, speaking: bool) -> None:
        # Called on the event loop by TTS. Speaker mode drops self-audio before
        # VAD/STT, including pre-roll and results already being recognized.
        with self._raw_lock:
            if speaking == self._speaking_out:
                return
            self._speaking_out = speaking
            if self.cfg["vad"].get("barge_in", False):
                self._echo_until = 0.0
                return
            self._echo_until = 0.0 if speaking else time.monotonic() + 0.5
            self.generation += 1
            self._raw.clear()
        self._discard_capture_events()

    def _self_audio(self) -> bool:
        return not self.cfg["vad"].get("barge_in", False) and (
            self._speaking_out or time.monotonic() < self._echo_until
        )

    def start(self) -> None:
        import mlx_whisper  # late import: loads Metal

        self._mlx = mlx_whisper
        if self.native_audio is not None:
            self.native_audio.start()
            if self._stop.is_set():
                self.native_audio.stop()
                return
            self.native_audio.mute(self._muted)
            self.device_name = self.native_audio.info.get('mic', 'System microphone (echo cancellation)')
            self._thread = threading.Thread(target=self._run, name='ouvido', daemon=True)
            self._thread.start()
            return
        device = self._resolve_device(self.cfg["audio"]["input_device"])
        self._stream = sd.InputStream(
            samplerate=self.sr, channels=1, dtype="int16", blocksize=self.chunk,
            device=device, callback=self._on_audio,
        )
        self._stream.start()
        info = sd.query_devices(self._stream.device)
        self.device_name = str(info["name"])
        if info["max_output_channels"] > 0 or any(k in self.device_name.lower() for k in ("blackhole", "loopback", "soundflower", "aggregate", "multi-output")):
            log.warning("WARNING: %r looks like a virtual/aggregate device; it may capture audio from other apps.", self.device_name)
        log.info("microphone: %s (input only; sound from other apps is not captured)", self.device_name)
        self._thread = threading.Thread(target=self._run, name="ouvido", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.set_muted(True)
        if self.native_audio is not None:
            self.native_audio.stop()
        if self._stream:
            self._stream.stop(); self._stream.close()
        self._stt.shutdown(wait=False)
        self._live.shutdown(wait=False)

    def set_device(self, spec: str | int | None) -> str:
        """Switch the microphone live (e.g. AirPods ↔ laptop microphone). Returns the name in use."""
        if self.native_audio is not None:
            raise RuntimeError('Echo-cancelled audio uses the microphone selected in macOS Sound settings; turn voice off before changing it.')
        device = self._resolve_device(spec if spec is not None else "")
        novo = sd.InputStream(samplerate=self.sr, channels=1, dtype="int16", blocksize=self.chunk, device=device, callback=self._on_audio)
        antigo, self._stream = self._stream, novo
        novo.start()
        if antigo:
            try:
                antigo.stop(); antigo.close()
            except Exception:  # noqa: BLE001
                pass
        with self._raw_lock:
            self._raw.clear()
        self.device_name = str(sd.query_devices(novo.device)["name"])
        log.info("microphone: %s", self.device_name)
        return self.device_name

    def set_muted(self, muted: bool) -> None:
        with self._raw_lock:
            if self._muted == muted:
                return
            self._muted = muted
            self.generation += 1
            self._raw.clear()
        self._discard_capture_events()
        if self.native_audio is not None:
            self.native_audio.mute(muted)

    def _discard_capture_events(self) -> None:
        # Called on the event-loop thread. Keep control events, discard capture events.
        keep = []
        while not self.queue.empty():
            event = self.queue.get_nowait()
            self.queue.task_done()
            if event[0] not in {"level", "live", "speech_start", "rec_end", "utterance", "speech_discarded"}:
                keep.append(event)
        for event in keep:
            self.queue.put_nowait(event)

    def set_ptt_mode(self, on: bool) -> None:
        self.ptt_mode = on
        self._ptt_down = False

    def ptt(self, down: bool) -> None:
        self._ptt_down = down

    def prompt_inicial(self) -> str | None:
        from .voices import prompt_para

        return self.cfg["stt"].get("initial_prompt") or prompt_para(self.cfg["tts"].get("lang", "pt_PT")) or None

    def transcrever(self, a) -> dict:
        """Transcribe a float32 16 kHz array (synchronous; call on the STT thread).

        Short clips (< 1.5 s: a "yes", a "no") go without an initial prompt and with a second of silence
        in front: with the prompt, Whisper returns pieces of it ("está bem, combinado") or completes the
        word ("simplesmente") instead of what was said."""
        curto = len(a) < 1.5 * self.sr
        if curto:
            a = np.concatenate([np.zeros(self.sr, dtype=np.float32), a, np.zeros(self.sr // 2, dtype=np.float32)])
        r = self._mlx.transcribe(
            a, path_or_hf_repo=self.cfg["stt"]["model"], language=self.cfg["stt"]["language"],
            fp16=True, condition_on_previous_text=False, initial_prompt=None if curto else self.prompt_inicial(),
            temperature=0.0 if curto else (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
        )
        if not curto:
            # never accept a piece of the initial prompt as a transcription
            prompt = (self.prompt_inicial() or "").lower()
            texto = (r.get("text") or "").strip().lower().strip(" .!?,")
            if prompt and texto and len(texto) > 8 and texto in prompt:
                r["text"] = ""
        return r

    def transcrever_async(self, a):
        """Loop-friendly version: runs on the STT thread and returns a Future."""
        return self.loop.run_in_executor(self._stt, self.transcrever, a)

    def warmup(self) -> None:
        """Load Whisper (and Parakeet for live text) before the first sentence."""
        silence = np.zeros(self.sr, dtype=np.float32)
        self._mlx.transcribe(silence, path_or_hf_repo=self.cfg["stt"]["model"], language=self.cfg["stt"]["language"], fp16=True)
        if self.cfg["stt"].get("live", True):
            # everything that touches Parakeet runs on the "live" thread: MLX streams are per thread
            def carregar() -> None:
                try:
                    from parakeet_mlx import from_pretrained

                    self._parakeet = from_pretrained(self.cfg["stt"].get("live_model", "mlx-community/parakeet-tdt-0.6b-v3"))
                    log.info("live text: Parakeet ready")
                except Exception as e:  # noqa: BLE001
                    log.warning("live text unavailable (%s)", e)
                    self._parakeet = None

            self._live.submit(carregar).result()

    # ---------- live text (streaming Parakeet) ----------
    def _live_start(self) -> None:
        self._live_epoch += 1
        if self._parakeet is None:
            return
        self._live.submit(self._live_open)

    def _live_open(self) -> None:
        try:
            self._live_stream = self._parakeet.transcribe_stream(context_size=(256, 256), depth=1).__enter__()
        except Exception as e:  # noqa: BLE001
            log.debug("live open: %s", e); self._live_stream = None

    def _live_feed(self, chunks: list[bytes], generation: int | None = None) -> None:
        """Feed ~0.5 s of audio to Parakeet and emit the partial (skipped if a request is still pending)."""
        if self._parakeet is None or self._live_pending > 0:
            return
        self._live_pending += 1
        raw = b"".join(chunks)
        generation = self.generation if generation is None else generation
        live_epoch = self._live_epoch

        def run() -> None:
            try:
                st = self._live_stream
                if st is None or live_epoch != self._live_epoch:
                    return
                import mlx.core as mx

                a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                st.add_audio(mx.array(a))
                text = (st.result.text or "").strip()
                if text:
                    self._emit("live", text, generation, live_epoch=live_epoch)
            except Exception as e:  # noqa: BLE001
                log.debug("live feed: %s", e)
            finally:
                self._live_pending -= 1

        self._live.submit(run)

    def _live_close(self) -> None:
        # The live worker may finish after Whisper or the UI has cleared Hearing.
        # Invalidate synchronously, including callbacks waiting on the event loop.
        self._live_epoch += 1
        def run() -> None:
            st, self._live_stream = self._live_stream, None
            if st is not None:
                try:
                    st.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass

        self._live.submit(run)

    # ---------- audio ----------
    @staticmethod
    def _resolve_device(spec: str | int):
        if spec == "" or spec is None:
            return None
        if isinstance(spec, int) or str(spec).isdigit():
            return int(spec)
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and str(spec).lower() in d["name"].lower():
                return i
        log.warning("microphone %r not found; using the default device", spec)
        return None

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            log.debug("audio status: %s", status)
        with self._raw_lock:
            if not self._muted and not self._stop.is_set() and not self._self_audio():
                self._raw.append(bytes(indata))

    def _next_chunk(self) -> bytes | None:
        with self._raw_lock:
            return self._raw.popleft() if self._raw else None

    # ---------- state machine ----------
    def recalc(self) -> None:
        """Recompute the parameters derived from the VAD settings; call after changing cfg['vad']."""
        v = self.cfg["vad"]
        chunk_ms = 1000 * self.chunk / self.sr
        self._p = {
            "pre_roll_n": max(1, int(v["pre_roll_ms"] / chunk_ms)),
            "min_speech_n": max(1, int(v["min_speech_ms"] / chunk_ms)),
            "barge_min_n": max(1, int(v["barge_in_min_ms"] / chunk_ms)),
            "silence_n": max(1, int(v["silence_ms"] / chunk_ms)),
            "max_n": int(v["max_utterance_s"] * 1000 / chunk_ms),
            "start": float(v["start_threshold"]),
            "end": float(v["end_threshold"]),
            "barge_thr": float(v["barge_in_threshold"]),
            "barge_in": bool(v["barge_in"]),
            "margin_db": float(v.get("noise_margin_db", 8.0)),
            "min_db": float(v.get("min_level_db", -55.0)),
        }

    def _speech_frame(self, prob: float, chunk: bytes, threshold: float) -> bool:
        """A frame counts as speech only if the VAD agrees and it is clearly louder than the room."""
        db = self.noise.level_db(chunk)
        if prob < self._p["end"] and not self.speaking_out:
            self.noise.observe_quiet(db)  # the assistant's own voice never raises the room floor
        loud = db >= max(self.noise.floor_db() + self._p["margin_db"], self._p["min_db"])
        return prob >= threshold and loud

    def _run(self) -> None:
        self.recalc()
        chunk_ms = 1000 * self.chunk / self.sr
        pre_roll: collections.deque = collections.deque(maxlen=self._p["pre_roll_n"])
        recording = False
        buf: list[bytes] = []
        speech_run = 0
        silence_run = 0
        level_tick = 0
        live_mark = 0
        generation = self.generation

        while not self._stop.is_set():
            if generation != self.generation:
                generation = self.generation
                if recording:
                    self._live_close()
                pre_roll.clear()
                recording = False; buf = []; speech_run = 0; silence_run = 0; live_mark = 0
                self.vad.reset()
            chunk = self._next_chunk()
            if chunk is None:
                time.sleep(0.005)
                continue
            if generation != self.generation or self._self_audio():
                continue
            if len(chunk) != self.chunk * 2:  # the VAD requires chunks of exactly 512 int16 samples
                chunk = (chunk + b"\x00" * (self.chunk * 2))[: self.chunk * 2]

            level_tick += 1
            if level_tick % 3 == 0:  # ~10 Hz
                a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                self._emit("level", float(min(1.0, np.sqrt(np.mean(a * a)) / 6000.0)), generation)

            if self._muted:
                if recording:
                    self._live_close()
                recording = False; buf = []; speech_run = 0
                continue

            # ----- push-to-talk: records only while held -----
            if self.ptt_mode:
                if self._ptt_down and not recording:
                    recording = True; buf = list(pre_roll); live_mark = 0; self._emit("speech_start", None, generation); self._live_start()
                elif not self._ptt_down and recording:
                    self._live_close(); self._finish(buf, chunk_ms, generation); recording = False; buf = []
                if recording:
                    buf.append(chunk)
                    if len(buf) - live_mark >= 16:
                        self._live_feed(buf[live_mark:], generation); live_mark = len(buf)
                else:
                    pre_roll.append(chunk)
                continue

            # ----- automatic: VAD -----
            p = self._p
            prob = self.vad(chunk)
            if not recording:
                pre_roll.append(chunk)
                if self.speaking_out and not p["barge_in"]:
                    speech_run = 0
                    continue
                thr = p["barge_thr"] if self.speaking_out else p["start"]
                need = p["barge_min_n"] if self.speaking_out else p["min_speech_n"]
                speech_run = speech_run + 1 if self._speech_frame(prob, chunk, thr) else 0
                if speech_run >= need:
                    recording = True; buf = list(pre_roll); silence_run = 0; live_mark = 0
                    self._emit("speech_start", None, generation)
                    self._live_start(); self._live_feed(buf, generation); live_mark = len(buf)
                continue

            buf.append(chunk)
            if len(buf) - live_mark >= 16:  # ~0.5 s of new audio
                self._live_feed(buf[live_mark:], generation); live_mark = len(buf)
            silence_run = silence_run + 1 if prob < p["end"] else 0
            if silence_run >= p["silence_n"] or len(buf) >= p["max_n"]:
                self._live_close()
                self._finish(buf, chunk_ms, generation)
                recording = False; buf = []; speech_run = 0

    def _finish(self, buf: list[bytes], chunk_ms: float, generation: int) -> None:
        if self._muted or self._self_audio() or generation != self.generation:
            return
        self.vad.reset()
        audio = b"".join(buf)
        self._emit("rec_end", len(buf) * chunk_ms / 1000, generation)
        self._stt.submit(self._transcribe, audio, generation)

    def _transcribe(self, raw: bytes, generation: int | None = None) -> None:
        generation = self.generation if generation is None else generation
        if self._muted or self._self_audio() or generation != self.generation or self._stop.is_set():
            return
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        dur = len(a) / self.sr
        if dur < self.cfg["stt"]["min_duration_s"]:
            self._emit("speech_discarded", f"Too short ({dur:.1f}s)", generation)
            return
        t0 = time.time()
        try:
            r = self.transcrever(a)
        except Exception as e:  # noqa: BLE001
            log.error("whisper: %s", e)
            self._emit("speech_discarded", f"Transcription failed: {e}", generation)
            return
        if self._muted or self._self_audio() or generation != self.generation or self._stop.is_set():
            return
        text = (r.get("text") or "").strip()
        segs = r.get("segments") or []
        motivo = self._suspeito(text, segs, dur)
        if motivo:
            log.debug("discarded (%s): %r", motivo, text)
            self._emit("speech_discarded", f"{motivo}: {text}", generation)
            return
        log.info("stt %.2fs (%.1fs audio, %d characters)", time.time() - t0, dur, len(text))
        log.debug("stt text: %s", text)
        lp = (sum(s.get("avg_logprob", 0) for s in segs) / len(segs)) if segs else 0.0
        self._emit("utterance", {"text": text, "audio": a, "dur": dur, "logprob": lp}, generation)

    @staticmethod
    def _suspeito(text: str, segs: list, dur: float) -> str | None:
        if not text:
            return "Empty transcript"
        if segs and all(s.get("no_speech_prob", 0) > 0.6 for s in segs):
            return "No speech detected"
        if segs:
            lp = sum(s.get("avg_logprob", 0) for s in segs) / len(segs)
            if lp < -1.0:
                return f"Low confidence ({lp:.2f})"
            if any(s.get("compression_ratio", 0) > 2.4 for s in segs):
                return "Repetitive transcript"
        limpo = text.strip(" .!?,").lower()
        if ALUCINACOES_RE.search(limpo):
            return "Likely hallucination"
        # A six-word question can fit comfortably into 1–2 seconds. Reject
        # implausible density, not a fixed word count below a duration cliff.
        words = len(limpo.split())
        if dur < 2.0 and words >= 6 and words > 6.0 * max(dur, 0.0):
            return "Implausible speech rate"
        return None

    def _emit(self, kind: str, payload: Any, generation: int | None = None, *, live_epoch: int | None = None) -> None:
        generation = self.generation if generation is None else generation
        def deliver():
            if live_epoch is not None and live_epoch != self._live_epoch:
                return
            if not self._muted and not self._self_audio() and not self._stop.is_set() and generation == self.generation:
                self.queue.put_nowait((kind, payload))
        self.loop.call_soon_threadsafe(deliver)


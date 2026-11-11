"""Voice daemon: listening + speech, without Claude. Meant to be used by a Claude desktop mod.

Protocol:
  stdout  — one JSON line per event: {"type": "ready"|"state"|"level"|"utterance"|"discarded"|"spoken"|"note", ...}
  HTTP    — POST /cmd to http://127.0.0.1:<port> with JSON:
              {"type":"say","text":...}      speak a text (split into sentences)
              {"type":"stop"}                stop speaking now and clear the queue
              {"type":"ptt","down":bool}     push-to-talk
              {"type":"mute","on":bool}
              {"type":"mode","mode":"auto"|"ptt"}
              {"type":"quit"}
  Every HTTP request needs the per-start secret in the X-Voz-Token header. With
  --token-stdin the daemon reads it from the first line of standard input (the
  plugin sends it there); without it the daemon makes its own random secret and
  the HTTP channel stays closed to everyone else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import secrets
import sys
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# Models are pinned and verified by setup (voz/models.py); the daemon never downloads.
os.environ["HF_HUB_OFFLINE"] = "1"

from .config import load_config
from .speech import Fala
from .rewriter import limpar_markdown
from .models import ModelError, check_configured
from .listener import Ouvido
from .ui import UI
from .private_files import open_private, private_dir

# The mod reads JSON events from the process stdout; everything else (library prints) goes to stderr.
_EVENTS = sys.stdout
sys.stdout = sys.stderr

log = logging.getLogger("voz.daemon")
_SENT_END = re.compile(r"(?<=[.!?…])\s+")
SAUDACAO = {
    "pt_PT": "Olá, agora falo com esta voz.", "pt_BR": "Olá, agora eu falo com esta voz.", "en": "Hello, this is my voice now.",
    "es": "Hola, ahora hablo con esta voz.", "fr": "Bonjour, je parle maintenant avec cette voix.", "de": "Hallo, jetzt spreche ich mit dieser Stimme.",
    "it": "Ciao, ora parlo con questa voce.", "nl": "Hallo, dit is nu mijn stem.", "pl": "Cześć, teraz mówię tym głosem.",
}
ACK = "/System/Library/Sounds/Tink.aiff"


# Short replies a user gives while the voice is still talking. They are clear on
# their own, so they skip the word-count and duration checks (the echo check still applies).
SHORT_COMMANDS = frozenset({
    "start it", "send", "send it", "do it", "yes", "yeah", "yep", "no", "go",
    "stop", "approve", "approve it", "next", "ready",
})


def _is_short_command(texto: str) -> bool:
    return " ".join(re.findall(r"[a-z']+", texto.lower())) in SHORT_COMMANDS


class Daemon:
    def __init__(self, cfg: dict, port: int, token: str = ""):
        self.cfg = cfg
        self.port = port
        self.token = token or secrets.token_urlsafe(32)
        self._hold = False
        self._pending: list[str] = []
        self._state = "idle"
        self._acumulado = ""
        self._segmentos: list = []  # audio of each sentence of the message in progress (for the final pass)
        self._gap_task: asyncio.Task | None = None
        self._speech_recording = False
        self._recognitions_pending = 0
        self._speech_revision = 0
        self.reuniao = None          # (Oradores, Acta) while in meeting mode
        self._mic_antes: str | None = None
        self._ditas: list[str] = []  # last sentences spoken by the voice (echo filter)
        self._muted = False
        self._capture_generation = 0
        self._closing = False
        self._microphone_stopped = False
        from .intent import Intencao

        kw = cfg.get("keywords", {})
        self._intencoes = Intencao(kw.get("model", "mlx-community/Qwen3-4B-4bit"), bool(kw.get("dynamic", True)), int(kw.get("max_words", 6)))
        self._interrompeu_voz = False
        self._state_antes_de_gravar = "listening"

    def _capture_current(self, generation: int) -> bool:
        return not self._muted and not self._closing and generation == self._capture_generation

    def _stop_microphone(self) -> None:
        if not self._microphone_stopped:
            self.ouvido.stop()
            self._microphone_stopped = True

    def _speech_busy(self) -> bool:
        return self._speech_recording or self._recognitions_pending > 0

    def _turn_open(self) -> bool:
        """True while the user talks, or has words that are not sent yet. The voice starts no reply then."""
        return self._hold or self._speech_busy() or bool(self._acumulado)

    def _speak_pending(self) -> None:
        """Give the held reply sentences to the voice, in order, when the user's turn is closed.

        2026-11-11: these sentences were deleted when the user's words were recognized, so a reply
        that arrived while the user talked was never heard."""
        if self._pending and not self._turn_open():
            held, self._pending = self._pending, []
            for sentence in held:
                self.fala.dizer(sentence)

    def _schedule_gap(self, generation: int) -> None:
        if self._gap_task:
            self._gap_task.cancel()
            self._gap_task = None
        if self._acumulado and self._capture_current(generation) and not self._speech_busy():
            self._gap_task = asyncio.create_task(self._enviar_apos_pausa(generation))

    async def _enviar_apos_pausa(self, generation: int | None = None) -> None:
        """Finalize only after the pause and every captured segment is resolved."""
        generation = self._capture_generation if generation is None else generation
        task = asyncio.current_task()
        try:
            await asyncio.sleep(int(self.cfg["vad"].get("turn_gap_ms", 2500)) / 1000)
            if not self._capture_current(generation) or self._speech_busy():
                return
            revision = self._speech_revision
            # Keep both the buffer and task registered until delivery commits.
            # A continuation can cancel cleanup/retranscription without losing
            # the earlier words, and its later result can join the same turn.
            texto = self._acumulado
            segmentos = list(self._segmentos)
            if not texto:
                return
            if len(segmentos) > 1:
                try:
                    import numpy as np

                    gap = np.zeros(int(self.ouvido.sr * 0.25), dtype=np.float32)
                    todo = np.concatenate([x for seg in segmentos for x in (seg, gap)])
                    if len(todo) / self.ouvido.sr <= 120:
                        r = await self.ouvido.transcrever_async(todo)
                        final = (r.get("text") or "").strip()
                        if final and len(final) >= 0.6 * len(texto):
                            texto = final
                except Exception as e:  # noqa: BLE001
                    log.warning("final pass failed: %s", e)
            if not self._capture_current(generation):
                return
            from .voices import aplicar_substituicoes

            texto = aplicar_substituicoes(texto)
            texto = await self._limpar(texto)
            if (not self._capture_current(generation) or self._speech_busy()
                    or revision != self._speech_revision):
                return
            self._acumulado = ""
            self._segmentos = []
            if self.cfg["ui"]["ack_sound"]:
                asyncio.create_task(self._ack())
            self.out("utterance", text=texto)
        except asyncio.CancelledError:
            return
        finally:
            if self._gap_task is task:
                self._gap_task = None
            self._speak_pending()

    def out(self, type_: str, **data) -> None:
        if type_ in {"utterance", "partial", "live", "keyword", "abort"}:
            data["generation"] = self._capture_generation
        _EVENTS.write(json.dumps({"type": type_, **data}, ensure_ascii=False) + "\n")
        _EVENTS.flush()
        if type_ != "level" or True:
            self.ui.emit(type_, **data)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.events: asyncio.Queue = asyncio.Queue()
        self.ouvido = Ouvido(self.cfg, self.loop, self.events)
        self.fala = Fala(self.cfg, on_state=self._on_speaking, on_error=lambda error: self.out("note", text=error))
        self.fala.audio_device = getattr(self.ouvido, "native_audio", None)
        self.ui = UI(self.port, self._on_cmd, self.token)
        try:
            await self.ui.start()
            if self.cfg["tts"]["backend"] in {"mlx", "kokoro", "turbo"}:
                # Keep HTTP controls responsive while the cold neural voice loads.
                warm = getattr(self.fala, {"mlx": "_carregar_mlx", "kokoro": "_carregar_kokoro", "turbo": "_carregar_turbo"}[self.cfg["tts"]["backend"]])
                await self.loop.run_in_executor(self.fala._mlx_pool, warm)
                if self._closing:
                    return
            self.fala.start()
            try:
                await self.loop.run_in_executor(None, self.ouvido.start)
            except Exception:
                if self._closing:
                    return
                raise
            if self._closing:
                return
            await self.loop.run_in_executor(None, self.ouvido.warmup)
            if self._closing:
                return
            if self._intencoes.dinamico:
                self._intencoes.pool.submit(self._intencoes.carregar)  # in the background: ready in ~20 s
            native = getattr(self.ouvido, 'native_audio', None)
            saida = native.info.get('output', self._saida_actual()) if native else self._saida_actual()
            try:
                with open_private(private_dir(Path.home() / ".voz") / "daemon.json") as state:
                    state.write(json.dumps({"port": self.port, "pid": os.getpid()}))
            except Exception:  # noqa: BLE001
                pass
            self.out("ready", mic=self.ouvido.device_name, output=saida, muted=self._muted, tts=f"{self.cfg['tts']['backend']}/{self.fala.voice}",
                     mode=self.cfg["vad"]["mode"], port=self.port, settings=self._definicoes(), voices=self.fala.vozes())
            self._set_state("listening")
            await self._main_loop()
        finally:
            self._closing = True
            self._capture_generation += 1
            self._stop_microphone()
            await self.fala.close()
            await self.ui.close()

    async def _main_loop(self) -> None:
        while True:
            kind, payload = await self.events.get()
            generation = self._capture_generation
            if kind not in {"quit", "fatal"} and not self._capture_current(generation):
                continue
            accao = None
            if kind == "utterance":
                self._recognitions_pending = max(1, self._recognitions_pending)
                self._speech_revision += 1
            if kind == "utterance" and self.reuniao is None:
                accao = await self._intencao(payload["text"] if isinstance(payload, dict) else str(payload))
                if not self._capture_current(generation):
                    continue
            if kind in {"utterance", "speech_discarded"}:
                self._recognitions_pending = max(0, self._recognitions_pending - 1)
            if kind == "fatal":
                raise RuntimeError(str(payload))
            if kind == "level":
                self.out("level", v=round(payload, 3))
            elif kind == "speech_start":
                self._speech_recording = True
                self._speech_revision += 1
                self._hold = True
                if self._gap_task:  # you kept talking: the previous sentence is not sent yet
                    self._gap_task.cancel(); self._gap_task = None
                self._set_state("recording")
                if self._state_antes_de_gravar == "speaking" or self.fala.busy:
                    # the voice was speaking: pause (it may be its own echo); decide once there is a transcription
                    self._interrompeu_voz = True
                    await self.fala.pause()
                else:
                    self._interrompeu_voz = False
                    await self.fala.stop()
            elif kind == "live":
                self.out("live", text=(self._acumulado + " " + payload).strip())
            elif kind == "rec_end":
                self._speech_recording = False
                self._recognitions_pending += 1
                self._speech_revision += 1
                self._set_state("transcribing")
            elif kind == "speech_discarded":
                self._hold = self._speech_busy()
                self.out("discarded", reason=payload)
                if self._interrompeu_voz:
                    self._interrompeu_voz = False
                    self.fala.resume()
                self._speak_pending()
                self._set_state("recording" if self._speech_recording else "transcribing" if self._recognitions_pending else "speaking" if (self.fala.busy or self.fala.paused) else "listening")
                self._schedule_gap(generation)
            elif kind == "utterance" and self.reuniao is not None:
                await self._reuniao_entrada(payload)
            elif kind == "utterance" and accao:
                self._hold = False; self._interrompeu_voz = False; self._pending.clear()
                if self._gap_task:
                    self._gap_task.cancel(); self._gap_task = None
                self._acumulado = ""; self._segmentos = []
                if accao == "skip":
                    await self.fala.skip()
                elif accao == "stop_voice":
                    await self.fala.stop()
                else:
                    await self.fala.stop()
                    if not self._capture_current(generation):
                        continue
                    self.out("abort")
                if not self._capture_current(generation):
                    continue
                self.out("keyword", action=accao)
                self._set_state("speaking" if accao == "skip" and self.fala.busy else "listening")
            elif kind == "utterance":
                texto = payload["text"] if isinstance(payload, dict) else str(payload)
                over_voice = self._interrompeu_voz
                if over_voice:
                    self._interrompeu_voz = False
                    motivo = self._interrupcao_falsa(payload, texto)
                    if motivo:
                        # that was not you (echo, breathing, noise, invented phrase): ignore it and resume reading
                        self._hold = False
                        self.out("discarded", reason=f"{motivo}: {texto[:60]}")
                        self.fala.resume()
                        self._speak_pending()
                        self._set_state("speaking")
                        continue
                self._hold = self._speech_busy()
                if over_voice:
                    # The user talked over an audible voice: the rest of that reply is dropped.
                    # A reply that the voice did not start stays in _pending and plays when the turn is sent.
                    self._pending.clear()
                await self.fala.stop()
                if not self._capture_current(generation):
                    continue
                # Joins consecutive sentences: only sends once you stop speaking for turn_gap_ms.
                if isinstance(payload, dict) and payload.get("audio") is not None:
                    self._segmentos.append(payload["audio"])
                self._acumulado = (self._acumulado + " " + texto).strip()
                self.out("partial", text=self._acumulado)
                self._set_state("recording" if self._speech_recording else "transcribing" if self._recognitions_pending else "listening")
                self._schedule_gap(generation)
            elif kind == "quit":
                return

    # ---------- meeting ----------
    async def _reuniao_entrada(self, payload) -> None:
        from .meeting import RE_NOME
        from .voices import aplicar_substituicoes

        generation = self._capture_generation
        oradores, acta = self.reuniao
        texto = payload["text"] if isinstance(payload, dict) else str(payload)
        texto = aplicar_substituicoes(texto)
        audio = payload.get("audio") if isinstance(payload, dict) else None
        idx = len(acta.entradas)
        if audio is None:
            nome, sim, novo = (oradores.nomes() or ["Orador 1"])[-1], 0.0, False
        else:
            nome, sim, novo = await self.loop.run_in_executor(None, oradores.identificar, audio, self.ouvido.sr, idx)
        if not self._capture_current(generation):
            return
        # "I am João" renames the speaker of this sentence
        m = RE_NOME.search(texto)
        if m and nome.startswith("Orador"):
            novo_nome = m.group(1).strip()
            if oradores.renomear(nome, novo_nome):
                acta.renomear(nome, novo_nome)
                self.out("meeting_speakers", speakers=oradores.nomes())
                nome = novo_nome
        e = acta.adicionar(nome, texto)
        self.out("meeting_entry", t=e["t"], who=nome, text=texto, similarity=round(sim, 2), new=novo, speakers=oradores.nomes())
        # every 10 contributions, regroup everything: fixes wrong merges and splits
        if len(acta.entradas) % 10 == 0:
            await self._reagrupar()

    async def _reagrupar(self) -> None:
        if self.reuniao is None:
            return
        oradores, acta = self.reuniao
        try:
            mapa = await self.loop.run_in_executor(None, oradores.reagrupar)
            mudou = acta.reatribuir(mapa) if mapa else 0
            self.out("meeting_speakers", speakers=oradores.nomes(), relabeled=mudou, entries=[{"t": e["t"], "who": e["who"], "text": e["text"]} for e in acta.entradas[-200:]])
        except Exception as ex:  # noqa: BLE001
            log.warning("regrouping failed: %s", ex)

    async def _reuniao(self, on: bool, titulo: str = "", k: int | None = None) -> dict:
        if on and self.reuniao is None:
            from .meeting import Acta, Oradores

            self.out("note", text="preparing meeting mode (speaker model)...")
            oradores = await self.loop.run_in_executor(None, Oradores, float(self.cfg.get("meeting", {}).get("threshold", 0.7)))
            oradores.k = k if k and k > 0 else None
            acta = Acta(titulo)
            self.reuniao = (oradores, acta)
            await self.fala.stop()
            # meeting microphone (the laptop's picks up the room; AirPods only pick up their wearer)
            mic = self.cfg.get("meeting", {}).get("input_device", "")
            if mic:
                try:
                    self._mic_antes = self.cfg["audio"]["input_device"]
                    self.out("note", text=f"meeting microphone: {self.ouvido.set_device(mic)}")
                except Exception as e:  # noqa: BLE001
                    self.out("note", text=f"could not change the microphone ({e}); keeping {self.ouvido.device_name}")
            # longer sentences before closing (people pause) and no joining
            self._vad_antes = dict(self.cfg["vad"])
            self.cfg["vad"]["silence_ms"] = int(self.cfg.get("meeting", {}).get("silence_ms", 900))
            self.cfg["vad"]["max_utterance_s"] = 30
            self.ouvido.recalc()
            self._set_state("listening")
            self.out("meeting", on=True, title=acta.titulo, path=str(acta.path), speakers=[])
            return {"ok": True, "on": True, "path": str(acta.path)}
        if not on and self.reuniao is not None:
            await self._reagrupar()  # final pass: number of speakers and definitive assignments
            oradores, acta = self.reuniao
            self.reuniao = None
            path = acta.fechar()
            if self._mic_antes is not None:
                try:
                    self.ouvido.set_device(self._mic_antes)
                except Exception:  # noqa: BLE001
                    pass
                self._mic_antes = None
            if getattr(self, "_vad_antes", None):
                self.cfg["vad"].update(self._vad_antes); self.ouvido.recalc()
            self.out("meeting", on=False, title=acta.titulo, path=str(path), speakers=oradores.nomes(), entries=len(acta.entradas))
            return {"ok": True, "on": False, "path": str(path), "speakers": oradores.nomes(), "entries": len(acta.entradas)}
        return {"ok": True, "on": self.reuniao is not None}

    # ---------- commands ----------
    async def _on_cmd(self, cmd: dict) -> None:
        t = cmd.get("type")
        if self._closing and t != "quit":
            return {"ok": False, "error": "Voice daemon is shutting down."}
        if t == "meeting":
            k = cmd.get("speakers")
            return await self._reuniao(bool(cmd.get("on")), str(cmd.get("title", "")), int(k) if k else None)
        if t == "meeting_rename" and self.reuniao is not None:
            oradores, acta = self.reuniao
            de, para = str(cmd.get("speaker", "")), str(cmd.get("name", ""))
            ok = oradores.renomear(de, para)
            if ok:
                acta.renomear(de, para)
                self.out("meeting_speakers", speakers=oradores.nomes())
            return {"ok": ok, "speakers": oradores.nomes()}
        if t == "say":
            for s in _SENT_END.split(limpar_markdown(str(cmd.get("text", "")))):
                s = s.strip()
                if not s:
                    continue
                self._ditas.append(s)
                del self._ditas[:-12]
                self.out("spoken", text=s)
                self._pending.append(s)
            self._speak_pending()
        elif t == "stop":
            self._pending.clear()
            await self.fala.stop()
            self._set_state("listening")
        elif t == "ptt":
            down = bool(cmd.get("down"))
            if down and not self.ouvido.ptt_mode:
                self.ouvido.set_ptt_mode(True); self._ptt_temp = True
            self.ouvido.ptt(down)
            if not down and getattr(self, "_ptt_temp", False) and self.cfg["vad"]["mode"] == "auto":
                self.loop.call_later(0.2, self.ouvido.set_ptt_mode, False); self._ptt_temp = False
        elif t == "mute":
            on = bool(cmd.get("on"))
            if on != self._muted:
                self._capture_generation += 1
            self._muted = on
            self.ouvido.set_muted(on)
            if on:
                # real pause: nothing that was in progress reaches Claude
                if self._gap_task:
                    self._gap_task.cancel(); self._gap_task = None
                self._acumulado = ""; self._segmentos = []; self._pending.clear(); self._hold = False
                self._speech_recording = False; self._recognitions_pending = 0; self._speech_revision += 1
                self._interrompeu_voz = False
                await self.fala.stop()
            self.out("mute", on=on)  # the daemon is the source of truth on the microphone pause
            self._set_state("muted" if on else "listening")
            return {"ok": True, "generation": self._capture_generation}
        elif t == "mode":
            mode = "ptt" if cmd.get("mode") == "ptt" else "auto"
            self.cfg["vad"]["mode"] = mode; self.ouvido.set_ptt_mode(mode == "ptt"); self.out("mode", mode=mode)
        elif t == "voices":
            return {"voices": self.fala.vozes(), "current": self.fala.voz_actual()}
        elif t == "voices_search":
            from .voices import procurar

            lang = cmd.get("lang") or self.cfg["tts"].get("lang", "pt_PT")
            if cmd.get("all"):
                lang = None
            res = await self.loop.run_in_executor(None, procurar, lang, cmd.get("query"), self.fala.refs())
            return {"voices": res, "lang": lang}
        elif t == "voice_install":
            self.out("note", text=f"installing and activating voice {cmd.get('id')}...")
            r = await self._definir({"voice": cmd.get("id")})
            return {**r, "voices": self.fala.vozes()}
        elif t == "languages":
            from .voices import LINGUAS

            return {"languages": [{"code": k, "label": v} for k, v in LINGUAS.items()]}
        elif t == "devices":
            return self._dispositivos()
        elif t == "get":
            return self._definicoes()
        elif t == "set":
            return await self._definir(cmd)
        elif t == "quit":
            self._closing = True
            self._capture_generation += 1
            if self._gap_task:
                self._gap_task.cancel(); self._gap_task = None
            self._acumulado = ""; self._segmentos = []; self._pending.clear(); self._hold = False
            self._stop_microphone()
            await self.fala.stop()
            # reason "handover": another session takes over the voice; this one's mod should not show an error
            self.out("bye", reason=str(cmd.get("reason") or ""))
            self.events.put_nowait(("quit", None))
        return None

    # ---------- live settings ----------
    SENSIBILIDADE = {  # start, end, min_speech_ms, noise_margin_db (how far above the room level speech must be)
        "baixa": (0.6, 0.4, 250, 10.0),
        "media": (0.45, 0.3, 150, 7.0),
        "alta": (0.3, 0.2, 60, 4.0),
    }

    async def _limpar(self, texto: str) -> str:
        """Filler-word cleanup according to the setting: off | regras | modelo."""
        from .cleanup import Modelo, regras

        modo = self.cfg.get("cleanup", {}).get("mode", "regras")
        if modo == "off":
            return texto
        limpo = regras(texto, self.cfg["tts"].get("lang", "pt_PT"))
        if modo == "modelo":
            if not hasattr(self, "_modelo_limpeza"):
                self._modelo_limpeza = Modelo(self.cfg.get("cleanup", {}).get("model", "mlx-community/Qwen3-4B-4bit"))
            try:
                limpo = await asyncio.wait_for(self.loop.run_in_executor(self._modelo_limpeza.pool, self._modelo_limpeza.limpar, limpo), timeout=4)
            except Exception as e:  # noqa: BLE001
                log.warning("model cleanup failed: %s", e)
        if limpo != texto:
            self.out("note", text=f"Cleaned: {limpo}")
        return limpo

    def _definicoes(self) -> dict:
        v = self.cfg["vad"]
        sens = "media"
        for k, (s, *_rest) in self.SENSIBILIDADE.items():
            if abs(v["start_threshold"] - s) < 0.01:
                sens = k
        return {
            "voice": self.fala.voz_actual(),
            "sensitivity": sens,
            "turn_gap_ms": int(v.get("turn_gap_ms", 2500)),
            "silence_ms": int(v["silence_ms"]),
            "mode": v["mode"],
            "barge_in": bool(v["barge_in"]),
            "speed": float(self.cfg["tts"].get("speed", 1.0)),
            "expression": self.cfg["tts"].get("expression", "restrained"),
            "language": self.cfg["tts"].get("lang", "pt_PT"),
            "cleanup": self.cfg.get("cleanup", {}).get("mode", "regras"),
            "input_device": str(self.cfg["audio"].get("input_device") or ""),
            "output_device": str(self.cfg["tts"].get("output_device") or ""),
        }

    # ---------- audio devices ----------
    @staticmethod
    def _saida_actual() -> str:
        try:
            import sounddevice as sd

            idx = sd.default.device[1]
            info = sd.query_devices(idx) if idx is not None and idx >= 0 else sd.query_devices(kind="output")
            return str(info["name"])
        except Exception:  # noqa: BLE001
            return "?"

    def _definir_saida(self, spec: str) -> str:
        """Choose the sound output for the Qwen3/Piper voices (sounddevice) and Apple (say -a). '' = default."""
        import sounddevice as sd

        if self.cfg['audio'].get('backend') == 'native' and spec:
            raise RuntimeError('Echo-cancelled audio uses macOS Sound output. Turn voice off before changing the system output.')

        if not spec:
            sd.default.device[1] = None
            self.cfg["tts"]["output_device"] = ""
            return self._saida_actual()
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and spec.lower() in str(d["name"]).lower():
                sd.default.device[1] = i
                self.cfg["tts"]["output_device"] = str(d["name"])
                return str(d["name"])
        raise RuntimeError(f"output {spec!r} not found")

    def _dispositivos(self) -> dict:
        import sounddevice as sd

        virtual = ("blackhole", "loopback", "soundflower", "aggregate", "multi-output", "agregado")
        ins, outs = [], []
        for d in sd.query_devices():
            nome = str(d["name"])
            etiqueta = nome + (" (virtual)" if any(k in nome.lower() for k in virtual) else "")
            if d["max_input_channels"] > 0 and all(x["id"] != nome for x in ins):
                ins.append({"id": nome, "label": etiqueta})
            if d["max_output_channels"] > 0 and all(x["id"] != nome for x in outs):
                outs.append({"id": nome, "label": etiqueta})
        return {"inputs": ins, "outputs": outs, "input": self.ouvido.device_name, "output": self._saida_actual()}

    async def _definir(self, cmd: dict) -> dict:
        from .voices import LINGUAS, QWEN_LANGS, familia

        error = None
        if "language" in cmd and (str(cmd["language"]) not in LINGUAS or familia(str(cmd["language"])) not in QWEN_LANGS):
            error = "That language is not supported by the available local neural voices."
        if "expression" in cmd and cmd["expression"] not in ("restrained", "conversational", "natural"):
            error = "Expression must be restrained, conversational, or natural."
        if "speed" in cmd:
            try:
                speed = float(cmd["speed"])
                if isinstance(cmd["speed"], bool) or not math.isfinite(speed):
                    raise ValueError
            except (TypeError, ValueError):
                error = "Speech speed must be a finite number."
        if error:
            self.out("note", text=error)
            return {"ok": False, "error": error, **self._definicoes()}
        v = self.cfg["vad"]
        mudou = []
        if "sensitivity" in cmd and cmd["sensitivity"] in self.SENSIBILIDADE:
            s, e, m, margin = self.SENSIBILIDADE[cmd["sensitivity"]]
            v["start_threshold"], v["end_threshold"], v["min_speech_ms"], v["noise_margin_db"] = s, e, m, margin
            mudou.append("sensitivity")
        if "turn_gap_ms" in cmd:
            v["turn_gap_ms"] = max(300, min(5000, int(cmd["turn_gap_ms"]))); mudou.append("pause")
        if "silence_ms" in cmd:
            v["silence_ms"] = max(300, min(3000, int(cmd["silence_ms"]))); mudou.append("silence")
        if "barge_in" in cmd:
            v["barge_in"] = bool(cmd["barge_in"]); mudou.append("interruption")
        if "mode" in cmd:
            mode = "ptt" if cmd["mode"] == "ptt" else "auto"
            v["mode"] = mode; self.ouvido.set_ptt_mode(mode == "ptt"); self.out("mode", mode=mode); mudou.append("mode")
        self.ouvido.recalc()
        if "language" in cmd:
            lang = str(cmd["language"])
            if lang in LINGUAS:
                self.cfg["tts"]["lang"] = lang
                self.cfg["stt"]["language"] = familia(lang)
                if familia(lang) != "pt":
                    self.cfg["stt"]["initial_prompt"] = ""
                mudou.append("language")
                # Kokoro and Turbo are English. Qwen covers the other
                # supported languages; never substitute an Apple or Piper voice.
                requested_voice = str(cmd.get("voice", ""))
                if familia(lang) != "en" and (requested_voice.startswith(("kokoro:", "turbo:")) or (not requested_voice and self.cfg["tts"]["backend"] in {"kokoro", "turbo"})):
                    cmd = {**cmd, "voice": "mlx:voice:Ryan"}
                    self.out("note", text=f"Using Qwen Ryan for {LINGUAS[lang]}.")
        if "cleanup" in cmd and cmd["cleanup"] in ("off", "regras", "modelo"):
            self.cfg.setdefault("cleanup", {})["mode"] = cmd["cleanup"]; mudou.append("cleanup")
            if cmd["cleanup"] == "modelo":
                from .cleanup import Modelo

                if not hasattr(self, "_modelo_limpeza"):
                    self._modelo_limpeza = Modelo(self.cfg.get("cleanup", {}).get("model", "mlx-community/Qwen3-4B-4bit"))
                self._modelo_limpeza.pool.submit(self._modelo_limpeza._carregar)  # preload
        quiet = bool(cmd.get("quiet"))  # restore saved settings: apply without saying anything
        if "input_device" in cmd and str(cmd["input_device"] or "") != str(self.cfg["audio"].get("input_device") or ""):
            try:
                nome = self.ouvido.set_device(str(cmd["input_device"] or ""))
                self.cfg["audio"]["input_device"] = str(cmd["input_device"] or "")
                mudou.append("microphone")
                self.out("devices", mic=nome, output=self._saida_actual())
            except Exception as e:  # noqa: BLE001
                self.out("note", text=f"could not change the microphone: {e}")
        if "output_device" in cmd and str(cmd["output_device"] or "") != str(self.cfg["tts"].get("output_device") or ""):
            try:
                nome = self._definir_saida(str(cmd["output_device"] or ""))
                mudou.append("output")
                self.out("devices", mic=self.ouvido.device_name, output=nome)
            except Exception as e:  # noqa: BLE001
                self.out("note", text=f"could not change the sound output: {e}")
        if "speed" in cmd:
            speed = max(0.6, min(1.6, float(cmd["speed"])))
            native = getattr(self.ouvido, "native_audio", None)
            if native is not None and native.info.get("voice_processing"):
                native.set_rate(speed)
            self.cfg["tts"]["speed"] = speed
            mudou.append("speed")
        if "expression" in cmd:
            self.cfg["tts"]["expression"] = cmd["expression"]
            mudou.append("expression")
        if "voice" in cmd:
            self.out("note", text="Changing voice…")
            try:
                desc = await self.fala.mudar_voz(str(cmd["voice"]))
                self.out("note", text=f"Voice: {desc}"); mudou.append("voice")
                if not quiet:
                    self.fala.dizer(SAUDACAO.get(self.cfg["tts"].get("lang", "pt_PT"), SAUDACAO["en"]))
            except Exception as e:  # noqa: BLE001
                error = f"Could not change voice: {e}"
                self.out("note", text=error)
                return {"ok": False, "error": error, "changed": mudou, **self._definicoes()}
        defs = self._definicoes()
        if mudou:
            self.out("settings", **defs)  # the mod stores them: a change made by voice carries over to the next session
        return {"ok": True, "changed": mudou, **defs}

    async def _ack(self) -> None:
        p = await asyncio.create_subprocess_exec("afplay", "-v", "0.4", ACK, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()

    def _set_state(self, s: str) -> None:
        if s == "recording":
            self._state_antes_de_gravar = self._state
        if s == "listening" and self._muted:
            s = "muted"  # with the microphone paused, "listening" is never shown
        self._state = s
        self.out("state", state=s)

    async def _intencao(self, texto: str) -> str | None:
        """skip | stop_voice | abort | None. Multilingual seeds answer immediately; short sentences with no seed go to the local model."""
        a = self._intencoes.por_sementes(texto)
        if a or not self._intencoes.dinamico or len(texto.split()) > self._intencoes.max_palavras:
            return a
        try:
            return await asyncio.wait_for(self.loop.run_in_executor(self._intencoes.pool, self._intencoes.por_modelo, texto), timeout=3)
        except Exception as e:  # noqa: BLE001
            log.warning("model intent failed: %s", e)
            return None

    def _interrupcao_falsa(self, payload, texto: str) -> str | None:
        """While the voice speaks, only a clear contribution interrupts it: >= 3 words, >= 1 s of audio,
        reasonable Whisper confidence, and different from what the voice was saying."""
        if _looks_like_echo(texto, self._ditas):
            return "Speaker echo"
        if _is_short_command(texto):
            return None
        if isinstance(payload, dict):
            dur = float(payload.get("dur") or 0)
            lp = float(payload.get("logprob") or 0)
            if dur and dur < float(self.cfg["vad"].get("interrupt_min_s", 1.0)):
                return f"Too short to interrupt ({dur:.1f}s)"
            if lp < -0.8:
                return f"Low confidence for interruption ({lp:.2f})"
        if len(texto.split()) < int(self.cfg["vad"].get("interrupt_min_words", 3)):
            return "Too few words to interrupt"
        return None

    def _on_speaking(self, speaking: bool) -> None:
        self.ouvido.speaking_out = speaking
        if speaking:
            self._set_state("speaking")
        elif self._state == "speaking":
            self._set_state("listening")


ECHO_RUN_WORDS = 3
ECHO_COVERAGE = 0.6
ECHO_RECENT_LINES = 6


def _words(text: str) -> list:
    return re.findall(r"\w+", text.lower())


def _looks_like_echo(text: str, said: list) -> bool:
    """True when the transcript repeats the voice's recent speech in order.

    A word counts as echoed when it sits in a run of ECHO_RUN_WORDS or more consecutive
    transcript words that also appear consecutively in the last ECHO_RECENT_LINES spoken
    lines. A shorter transcript must appear whole. Echo needs ECHO_COVERAGE of the words.
    Shared topic words alone never count: replies reuse them.
    """
    words = _words(text)
    if not words:
        return True
    spoken = _words(" ".join(said[-ECHO_RECENT_LINES:]))
    if not spoken:
        return False
    run = min(ECHO_RUN_WORDS, len(words))
    spoken_ngrams = {tuple(spoken[i:i + run]) for i in range(len(spoken) - run + 1)}
    covered = [False] * len(words)
    for i in range(len(words) - run + 1):
        if tuple(words[i:i + run]) in spoken_ngrams:
            covered[i:i + run] = [True] * run
    return sum(covered) / len(words) >= ECHO_COVERAGE


def read_token(stream) -> str:
    """The secret the plugin writes on standard input. Fail fast when it is missing or short."""
    token = (stream.readline() or "").strip()
    if len(token) < 32:
        raise SystemExit("voz-daemon: --token-stdin needs a secret of at least 32 characters on standard input")
    return token


def main() -> None:
    p = argparse.ArgumentParser(prog="voz-daemon", description="Local listening and speech for the Claude desktop mod.")
    p.add_argument("--port", type=int, default=0, help="HTTP port (default: ui.port from the config)")
    p.add_argument("--config")
    p.add_argument("--ptt", action="store_true")
    p.add_argument("--tts", choices=["mlx", "kokoro", "turbo"])
    p.add_argument("--token-stdin", action="store_true", help="read the HTTP control secret from the first line of standard input")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    token = read_token(sys.stdin) if args.token_stdin else ""
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "httpcore", "huggingface_hub", "urllib3", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config(Path(args.config) if args.config else None)
    try:
        check_configured(cfg)
    except ModelError as e:
        _EVENTS.write(json.dumps({"type": "fatal", "error": str(e)}, ensure_ascii=False) + "\n")
        _EVENTS.flush()
        raise SystemExit(f"voz-daemon: {e}") from e
    if args.ptt:
        cfg["vad"]["mode"] = "ptt"
    if args.tts:
        cfg["tts"]["backend"] = args.tts
    try:
        asyncio.run(Daemon(cfg, args.port or int(cfg["ui"]["port"]), token).run())
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        logging.getLogger("voz").exception("the daemon exited with an error")
        _EVENTS.write(json.dumps({"type": "fatal", "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n")
        _EVENTS.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()


"""voz — voice conversation with Claude Code, everything local except Claude.

Microphone → Silero VAD → Whisper (MLX) → Claude Agent SDK (streaming) → TTS (say/mlx/piper).
Optional: Falante (LM Studio) rewrites long answers. Local control channel (secret-protected).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# Models are pinned and verified by setup (voz/models.py); this app never downloads.
os.environ["HF_HUB_OFFLINE"] = "1"

from .brain import Cerebro
from .config import load_config
from .speech import Fala
from .rewriter import Falante, limpar_markdown
from .listener import Ouvido
from .private_files import open_private, private_dir
from .ui import UI

log = logging.getLogger("voz")
ESTADO = Path.home() / ".voz"
ACK = "/System/Library/Sounds/Tink.aiff"

# Short acknowledgements spoken while Claude works: they are not a new message.
BACKCHANNEL = {"ok", "okay", "sim", "hum", "hmm", "certo", "está bem", "ok continua", "continua", "boa", "exacto", "exato", "pois"}
_SENT_END = re.compile(r"(?<=[.!?…])\s+")


def _say(msg: str) -> None:
    print(msg, flush=True)


class App:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg = load_config(Path(args.config) if args.config else None)
        if args.no_barge_in:
            self.cfg["vad"]["barge_in"] = False
        if args.falante:
            self.cfg["falante"]["enabled"] = True
        if args.ptt:
            self.cfg["vad"]["mode"] = "ptt"
        if args.permission_mode:
            self.cfg["claude"]["permission_mode"] = args.permission_mode
        if args.model:
            self.cfg["claude"]["model"] = args.model
        if args.tts:
            self.cfg["tts"]["backend"] = args.tts
        self.cwd = Path(args.cwd).resolve()
        self._turn: asyncio.Task | None = None
        self._pergunta: asyncio.Future | None = None
        self._hold = False
        self._pending: list[str] = []
        self._state = "idle"
        self._custo_acumulado = 0.0

    # ---------- startup ----------
    def _resolve_resume(self) -> str | None:
        r = self.args.resume
        if r == "last":
            f = ESTADO / "sessions.json"
            return json.loads(f.read_text()).get(str(self.cwd)) if f.is_file() else None
        return r

    def _guardar_sessao(self) -> None:
        if not self.cerebro.session_id:
            return
        private_dir(ESTADO)
        f = ESTADO / "sessions.json"
        data = json.loads(f.read_text()) if f.is_file() else {}
        data[str(self.cwd)] = self.cerebro.session_id
        with open_private(f) as out:
            out.write(json.dumps(data, indent=2))

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.events: asyncio.Queue = asyncio.Queue()
        self.ouvido = Ouvido(self.cfg, self.loop, self.events)
        self.fala = Fala(self.cfg, on_state=self._on_speaking, on_error=lambda error: self._note(error, warn=True))
        self.fala.audio_device = self.ouvido.native_audio
        self.falante = Falante(self.cfg)
        self.cerebro = Cerebro(self.cfg, self.cwd, self._resolve_resume(), self._perguntar)
        self.ui = UI(self.cfg["ui"]["port"], self._on_ui_cmd, secrets.token_urlsafe(32))

        _say(f"voz · folder {self.cwd} · config {self.cfg.get('_path', 'defaults')}")
        if self.cfg["ui"]["enabled"]:
            await self.ui.start()
            _say(f"panel: http://localhost:{self.cfg['ui']['port']}")
        if self.cfg["tts"]["backend"] in {"mlx", "kokoro", "turbo"}:
            warm = getattr(self.fala, {"mlx": "_carregar_mlx", "kokoro": "_carregar_kokoro", "turbo": "_carregar_turbo"}[self.cfg["tts"]["backend"]])
            await self.loop.run_in_executor(self.fala._mlx_pool, warm)
        self.fala.start()
        if self.cfg["falante"]["enabled"] and not await self.falante.disponivel():
            self._note("falante: LM Studio has no model loaded; turned off.", warn=True)
            self.cfg["falante"]["enabled"] = False
        t0 = time.time()
        await self.loop.run_in_executor(None, self.ouvido.start)
        await self.loop.run_in_executor(None, self.ouvido.warmup)
        _say(f"whisper ready in {time.time() - t0:.1f}s · microphone: {self.ouvido.device_name}")
        await self.cerebro.connect()
        _say("claude connected" + (f" (session {self.cerebro.session_id})" if self.cerebro.session_id else ""))
        self.ui.emit("hello", mic=self.ouvido.device_name, model=self.cerebro.model, mode=self.cfg["vad"]["mode"],
                     session=self.cerebro.session_id, tts=f"{self.cfg['tts']['backend']}/{self.fala.voice}")
        self._set_state("listening")
        await self._stdin_reader()
        _say("listening. Speak, or type and press Enter. Commands: /sair /mudo /ouvir /parar /ptt /auto")

        if self.args.prompt_file or self.args.prompt:
            texto = Path(self.args.prompt_file).read_text() if self.args.prompt_file else self.args.prompt
            self._start_turn(texto)
        try:
            await self._main_loop()
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        self._guardar_sessao()
        self.ouvido.stop()
        await self.fala.close()
        await self.falante.aclose()
        await self.ui.close()
        try:
            await self.cerebro.close()
        except Exception:  # noqa: BLE001
            pass
        _say("see you soon.")

    # ---------- main loop ----------
    async def _main_loop(self) -> None:
        while True:
            kind, payload = await self.events.get()
            if kind == "level":
                self.ui.emit("level", v=payload)
            elif kind == "speech_start":
                self._hold = True
                self._set_state("recording")
                await self.fala.stop()
            elif kind == "rec_end":
                self._set_state("transcribing")
            elif kind == "speech_discarded":
                self._hold = False
                self._note(f"ignored ({payload})")
                for frase in self._pending:
                    self.fala.dizer(frase)
                self._pending.clear()
                self._set_state("speaking" if not self.fala.queue.empty() else ("thinking" if self._turno_activo() else "listening"))
            elif kind == "utterance":
                self._hold = False
                if self.cfg["ui"]["ack_sound"]:
                    asyncio.create_task(self._ack())
                await self._on_user_text(payload, spoken=True)
            elif kind == "typed":
                if await self._comando(payload):
                    return
                await self._on_user_text(payload, spoken=False)
            elif kind == "quit":
                return

    def _turno_activo(self) -> bool:
        return self._turn is not None and not self._turn.done()

    async def _on_user_text(self, texto: str, spoken: bool) -> None:
        _say(f"\n› {texto}")
        if self._pergunta and not self._pergunta.done():
            if not spoken:
                # Only the microphone answers a permission question; typed or remote text never does.
                self._note("answer the permission question by voice; typed text does not count", warn=True)
                return
            self.ui.emit("user", text=texto)
            self._pergunta.set_result(texto)  # spoken answer to a permission request
            return
        if self._turno_activo() and texto.strip(" .!?,").lower() in BACKCHANNEL:
            self._note("acknowledgement; Claude continues")
            for frase in self._pending:
                self.fala.dizer(frase)
            self._pending.clear()
            self._set_state("thinking")
            return
        self.ui.emit("user", text=texto)
        self._pending.clear()
        await self.fala.stop()
        if self._turno_activo():
            await self.cerebro.interrupt()
            try:
                await self._turn
            except Exception:  # noqa: BLE001
                pass
        self._start_turn(texto)

    def _start_turn(self, texto: str) -> None:
        self._turn = asyncio.create_task(self._run_turn(texto), name="turno")

    def _falar(self, frase: str) -> None:
        frase = frase.strip()
        if not frase:
            return
        _say(f"  ♪ {frase}")
        self.ui.emit("spoken", text=frase)
        if self._hold:
            self._pending.append(frase)
        else:
            self.fala.dizer(frase)

    async def _run_turn(self, texto: str) -> None:
        t0 = time.time()
        self._set_state("thinking")
        buf = ""  # streaming text that has not reached a sentence end yet
        directo = not self.cfg["falante"]["enabled"]
        try:
            async for kind, payload in self.cerebro.turno(texto):
                if kind == "delta":
                    self.ui.emit("delta", text=payload)
                    if directo:
                        buf += payload
                        parts = _SENT_END.split(buf)
                        if len(parts) > 1:
                            for s in parts[:-1]:
                                self._falar(limpar_markdown(s))
                            buf = parts[-1]
                elif kind == "block_end":
                    self.ui.emit("block_end")
                    if directo and buf.strip():
                        self._falar(limpar_markdown(buf))
                    buf = ""
                elif kind == "text":
                    _say(f"\n◆ {payload.strip()}\n")
                    if not directo:
                        async for frase in self.falante.falar(payload):
                            self._falar(frase)
                elif kind == "tool":
                    _say(f"  ⚙ {payload}")
                    self.ui.emit("tool", text=payload)
                elif kind == "done":
                    r = payload
                    # total_cost_usd is the session total, in API-equivalent terms. With a claude.ai login
                    # (subscription) it is not charged; it counts toward the plan limits.
                    total = getattr(r, "total_cost_usd", None) or 0.0
                    delta = total - self._custo_acumulado
                    self._custo_acumulado = total
                    custo = f"≈${delta:.3f} this turn (${total:.2f} in the session, estimate; covered by the subscription)" if total else ""
                    _say(f"  ✓ {time.time() - t0:.1f}s · {r.num_turns} steps" + (f" · {custo}" if custo else ""))
                    self.ui.emit("done", secs=round(time.time() - t0, 1), turns=r.num_turns, cost=custo)
                    self._guardar_sessao()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("turn failed")
            self._note(f"error in the turn: {e}", warn=True)
            self.fala.dizer("There was an error talking to Claude. Check the panel.")
        finally:
            if not self.fala.queue.empty() or self._state == "speaking":
                pass  # the state becomes "listening" when the voice finishes (on_state)
            else:
                self._set_state("listening")

    async def _perguntar(self, pergunta: str) -> str:
        """Permission request: speak the question and wait for the next sentence."""
        _say(f"\n? {pergunta}")
        self.ui.emit("note", text=f"? {pergunta}")
        await self.fala.stop()
        self.fala.dizer(pergunta)
        self._pergunta = self.loop.create_future()
        try:
            return await asyncio.wait_for(self._pergunta, timeout=120)
        except asyncio.TimeoutError:
            return "no"
        finally:
            self._pergunta = None

    async def _ack(self) -> None:
        p = await asyncio.create_subprocess_exec("afplay", "-v", "0.4", ACK, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await p.wait()

    # ---------- state / panel ----------
    def _set_state(self, s: str) -> None:
        self._state = s
        self.ui.emit("state", state=s)

    def _note(self, text: str, warn: bool = False) -> None:
        _say(("  ! " if warn else "  · ") + text)
        self.ui.emit("note", text=text, warn=warn)

    def _on_speaking(self, speaking: bool) -> None:
        self.ouvido.speaking_out = speaking
        if speaking:
            self._set_state("speaking")
        elif self._state == "speaking":
            self._set_state("thinking" if self._turno_activo() else "listening")

    async def _on_ui_cmd(self, cmd: dict) -> None:
        t = cmd.get("type")
        if t == "ptt":
            down = bool(cmd.get("down"))
            if down and not self.ouvido.ptt_mode:
                self.ouvido.set_ptt_mode(True); self._ptt_temp = True
            self.ouvido.ptt(down)
            if not down and getattr(self, "_ptt_temp", False) and self.cfg["vad"]["mode"] == "auto":
                # go back to automatic after the recording closes
                self.loop.call_later(0.2, self.ouvido.set_ptt_mode, False); self._ptt_temp = False
        elif t == "mute":
            on = bool(cmd.get("on")); self.ouvido.set_muted(on); self._set_state("muted" if on else "listening")
        elif t == "stop":
            await self.fala.stop(); await self.cerebro.interrupt(); self._note("stopped")

    # ---------- keyboard ----------
    async def _stdin_reader(self) -> None:
        def ler() -> None:
            while True:
                line = sys.stdin.readline()
                if not line:
                    self.loop.call_soon_threadsafe(self.events.put_nowait, ("quit", None)); return
                line = line.strip()
                if line:
                    self.loop.call_soon_threadsafe(self.events.put_nowait, ("typed", line))

        threading.Thread(target=ler, name="stdin", daemon=True).start()

    async def _comando(self, line: str) -> bool:
        """Returns True to quit."""
        if line in ("/sair", "/exit", "/quit"):
            return True
        if line == "/mudo":
            self.ouvido.set_muted(True); self._set_state("muted")
        elif line == "/ouvir":
            self.ouvido.set_muted(False); self._set_state("listening")
        elif line == "/parar":
            await self.fala.stop(); await self.cerebro.interrupt(); self._note("stopped")
        elif line == "/ptt":
            self.cfg["vad"]["mode"] = "ptt"; self.ouvido.set_ptt_mode(True); self.ui.emit("mode", mode="ptt"); self._note("push-to-talk mode")
        elif line == "/auto":
            self.cfg["vad"]["mode"] = "auto"; self.ouvido.set_ptt_mode(False); self.ui.emit("mode", mode="auto"); self._note("automatic mode")
        else:
            return False
        self.events.put_nowait(("noop", None))
        return False


TEXTO_REF = (
    "Good afternoon. Today we will review the proposal decisions one at a time, and at the end I will record everything in the document. "
    "Tell me what you prefer on each point, and if you have any doubts, feel free to ask."
)


def gravar_referencia(cfg: dict, segundos: int = 15) -> None:
    """Record your voice as a cloning reference in ~/.voz/ref.wav + ref.txt."""
    import numpy as np
    import sounddevice as sd
    import soundfile as sf

    dev = Ouvido._resolve_device(cfg["audio"]["input_device"])
    nome = sd.query_devices(dev if dev is not None else sd.default.device[0])["name"]
    _say(f"microphone: {nome}\nRead aloud, naturally, for {segundos} s:\n\n  «{TEXTO_REF}»\n")
    input("Press Enter to start recording... ")
    sr = 24000
    audio = sd.rec(int(segundos * sr), samplerate=sr, channels=1, dtype="float32", device=dev)
    for i in range(segundos, 0, -1):
        print(f"\r  recording... {i:2d}s ", end="", flush=True); time.sleep(1)
    sd.wait(); print()
    a = audio[:, 0]
    # trim silence at both ends
    nz = np.where(np.abs(a) > 0.01)[0]
    if len(nz):
        a = a[max(0, nz[0] - sr // 4): min(len(a), nz[-1] + sr // 4)]
    private_dir(ESTADO)
    with open_private(ESTADO / "ref.wav", "wb") as f:
        sf.write(f, a, sr, format="WAV")
    _say("transcribing to confirm...")
    import mlx_whisper
    txt = mlx_whisper.transcribe(ESTADO / "ref.wav", path_or_hf_repo=cfg["stt"]["model"], language=cfg["stt"]["language"], fp16=True)["text"].strip()
    with open_private(ESTADO / "ref.txt") as f:
        f.write(txt)
    _say(f"saved to {ESTADO / 'ref.wav'} ({len(a) / sr:.1f}s)\ntranscript: {txt}\n"
         "If the transcript is wrong, fix ~/.voz/ref.txt. From now on the mlx backend uses this voice.")


def main() -> None:
    p = argparse.ArgumentParser(prog="voz", description="Voice conversation with Claude Code (local STT/TTS).")
    p.add_argument("--gravar-ref", action="store_true", help="record 15 s of your voice as a TTS reference and exit")
    p.add_argument("--cwd", default=".", help="project folder (default: current)")
    p.add_argument("--resume", help="Claude session id, or 'last' for the last one in this folder")
    p.add_argument("--prompt", help="first message")
    p.add_argument("--prompt-file", help="file containing the first message")
    p.add_argument("--config", help="path to config.toml")
    p.add_argument("--model", help="Claude model (sonnet, opus, haiku or full id)")
    p.add_argument("--tts", choices=["mlx", "kokoro", "turbo"], help="local neural speech engine")
    p.add_argument("--permission-mode", choices=["default", "plan"])
    p.add_argument("--ptt", action="store_true", help="push-to-talk: record only while you hold the key")
    p.add_argument("--no-barge-in", action="store_true")
    p.add_argument("--falante", action="store_true", help="rewrite answers with LM Studio")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpx2", "httpcore", "claude_agent_sdk", "huggingface_hub", "urllib3", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.gravar_ref:
        gravar_referencia(load_config(Path(args.config) if args.config else None))
        return
    try:
        asyncio.run(App(args).run())
    except KeyboardInterrupt:
        pass


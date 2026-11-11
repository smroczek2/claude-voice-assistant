"""Command intent in a short utterance, in any language.

Duas camadas:
  1. seeds: exact phrases in several languages, immediate answer;
  2. model: for short utterances (<= 6 words) that match no seed, the local Qwen3 classifies them as
     skip (skip the sentence the voice is saying), stop_voice (silence the voice, the work continues),
     abort (silence it and stop the work) or none (not a command).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("voz.intent")

ACCOES = ("skip", "stop_voice", "abort")

SEMENTES: dict[str, list[str]] = {
    "skip": [
        # pt
        "passa à frente", "passa a frente", "passa", "avança", "salta", "salta esta", "próximo", "próxima", "seguinte",
        # en
        "skip", "next", "skip this", "skip that", "move on", "go on",
        # es
        "siguiente", "salta", "pasa", "adelante",
        # fr
        "suivant", "suivante", "passe", "saute",
        # de
        "weiter", "überspringen", "nächste", "nächster",
        # it
        "avanti", "salta", "prossimo", "prossima",
    ],
    "stop_voice": [
        "interrompe", "interromper", "termina", "terminar", "cala-te", "cala te", "silêncio", "pára de falar", "para de falar", "chega", "basta",
        "shh", "stop talking", "be quiet", "quiet", "silence", "shut up", "enough", "that's enough",
        "cállate", "callate", "silencio", "deja de hablar", "ya basta",
        "tais-toi", "arrête de parler", "chut", "ça suffit",
        "sei still", "ruhe", "hör auf zu reden", "genug",
        "zitto", "silenzio", "smetti di parlare",
    ],
    "abort": [
        "pára", "para", "parar", "pára tudo", "para tudo", "cancela", "cancelar", "esquece",
        "stop", "cancel", "abort", "never mind", "forget it", "stop everything",
        "cancela", "cancelar", "detente", "para todo", "olvídalo",
        "annule", "arrête", "arrête tout", "laisse tomber",
        "abbrechen", "stopp", "halt", "vergiss es",
        "annulla", "ferma", "fermati", "lascia stare",
    ],
}

SISTEMA = (
    "You classify a very short spoken utterance addressed to a voice assistant that is currently reading an answer aloud. "
    "Answer with exactly one word: skip (the user wants to skip the sentence being read / go to the next), "
    "silence (the user wants the voice to stop reading but the work to continue), "
    "abort (the user wants to stop everything / cancel the task), or none (it is a normal message, question or statement). "
    "The utterance may be in any language. Reply with only: skip, silence, abort or none."
)
FEW = [
    ("passa à frente", "skip"), ("skip this part", "skip"), ("weiter bitte", "skip"), ("siguiente", "skip"),
    ("cala-te", "silence"), ("okay stop talking", "silence"), ("tais-toi s'il te plaît", "silence"), ("basta así", "silence"),
    ("pára tudo", "abort"), ("cancel that", "abort"), ("annule tout", "abort"), ("brich ab", "abort"),
    ("qual é o ponto de situação", "none"), ("sim", "none"), ("what about the second option", "none"), ("não concordo", "none"),
]
MAPA = {"skip": "skip", "silence": "stop_voice", "abort": "abort"}


def _norm(texto: str) -> str:
    t = re.sub(r"[.!?,;:¡¿\"']", " ", texto.lower())
    return re.sub(r"\s+", " ", t).strip()


class Intencao:
    def __init__(self, modelo: str = "mlx-community/Qwen3-4B-4bit", dinamico: bool = True, max_palavras: int = 6):
        self.modelo = modelo
        self.dinamico = dinamico
        self.max_palavras = max_palavras
        self._model = None
        self._tok = None
        self._lock = threading.Lock()
        self._cache: dict[str, str | None] = {}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="intencao")
        self._sementes = {frase: accao for accao, frases in SEMENTES.items() for frase in frases}

    # ---------- layer 1: seeds ----------
    def por_sementes(self, texto: str) -> str | None:
        return self._sementes.get(_norm(texto))

    # ---------- layer 2: model ----------
    def carregar(self) -> None:
        with self._lock:
            if self._model is None:
                from mlx_lm import load

                t = time.time()
                self._model, self._tok = load(self.modelo)
                log.info("intent classifier %s loaded in %.1fs", self.modelo, time.time() - t)

    def por_modelo(self, texto: str) -> str | None:
        """Synchronous (run in the pool). Short utterances only; returns the action or None."""
        t = _norm(texto)
        if not t or len(t.split()) > self.max_palavras:
            return None
        if t in self._cache:
            return self._cache[t]
        from mlx_lm import generate

        self.carregar()
        msgs = [{"role": "system", "content": SISTEMA}]
        for u, a in FEW:
            msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
        msgs.append({"role": "user", "content": t})
        prompt = self._tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        out = generate(self._model, self._tok, prompt=prompt, max_tokens=4, verbose=False).strip().lower()
        label = re.sub(r"[^a-z]", "", out.split()[0]) if out else "none"
        accao = MAPA.get(label)
        self._cache[t] = accao
        return accao

    def classificar(self, texto: str) -> str | None:
        """Seeds first, then the model (if enabled). Synchronous."""
        a = self.por_sementes(texto)
        if a or not self.dinamico:
            return a
        try:
            return self.por_modelo(texto)
        except Exception as e:  # noqa: BLE001
            log.warning("intent classification failed: %s", e)
            return None


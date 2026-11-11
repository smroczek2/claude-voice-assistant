"""Filler-word cleanup for the final transcript.

Two levels:
  "regras"  — deterministic and instant: filler words (um, uh, like, you know, okay…), repeated words,
              false starts ("I wanted... I wanted"), spaces and punctuation.
  "modelo"  — the above plus a small MLX model (few-shot) for corrections spoken midway
              ("X, no, I mean Y"). The model output is only accepted if it passes the guards:
              not longer than the original, no invented words, same final punctuation.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("voz.cleanup")

MULETAS = {
    "pt": ["hum", "hmm", "humm", "ah", "ahh", "eh", "ehh", "é assim", "tipo", "pronto", "ok", "okay", "então", "portanto", "pá", "bem"],
    "en": ["um", "uh", "hmm", "like", "you know", "okay", "so", "well", "basically", "actually"],
    "es": ["eh", "em", "pues", "bueno", "o sea", "vale", "tipo"],
    "fr": ["euh", "hum", "bon", "ben", "genre", "voilà", "du coup"],
    "de": ["ähm", "äh", "hm", "also", "halt", "quasi", "sozusagen"],
    "it": ["ehm", "eh", "cioè", "tipo", "allora", "insomma", "praticamente"],
}
# fillers that are only removed at the start of a sentence (mid-sentence they may be real words)
SO_INICIO = {"então", "portanto", "pronto", "ok", "okay", "bem", "so", "well", "pues", "bueno", "bon", "also", "allora"}

SISTEMA = (
    "You are a voice transcript corrector. Return the same text without filler words, repetitions or false starts, "
    "and with corrections spoken midway applied ('X, no, I mean Y' becomes Y). Do not change the meaning, do not answer "
    "questions, do not add anything, do not translate, keep the language and spelling. Output only the text."
)
FEW = [
    ("hum, eu acho que, eu acho que devíamos, tipo, começar pelo portal, não, quero dizer pelo orquestrador.", "Eu acho que devíamos começar pelo orquestrador."),
    ("pronto, ok, então a reunião fica para terça, ah, às dez.", "A reunião fica para terça às dez."),
    ("diz-me então qual é o ponto de situação do projecto.", "Diz-me então qual é o ponto de situação do projecto."),
    ("um, so I think we should, like, ship it on Friday, no, I mean Thursday.", "I think we should ship it on Thursday."),
]


def _familia(lang: str) -> str:
    return (lang or "pt").replace("-", "_").split("_")[0].lower()


def _falsos_comecos(t: str) -> str:
    """Remove a group of 1–4 words that is repeated right after itself (with or without an ellipsis between)."""
    palavras = re.findall(r"\S+", t)
    out: list[str] = []
    i = 0
    norm = lambda w: re.sub(r"[^\w]", "", w.lower())
    while i < len(palavras):
        achou = False
        for n in (4, 3, 2, 1):
            a = palavras[i:i + n]
            b = palavras[i + n:i + 2 * n]
            if len(b) == n and [norm(x) for x in a] == [norm(x) for x in b] and any(norm(x) for x in a):
                i += n  # skip the first copy
                achou = True
                break
        if not achou:
            out.append(palavras[i]); i += 1
    return " ".join(out)


def regras(texto: str, lang: str = "pt") -> str:
    t = " " + texto.strip() + " "
    fam = _familia(lang)
    muletas = MULETAS.get(fam, MULETAS["pt"])
    # sentence-initial fillers (after . ! ? or at the start), with an optional comma
    for m in muletas:
        t = re.sub(r"(?i)(^|[.!?]\s+)\s*" + re.escape(m) + r"[,]?\s+", r"\1", t)
    # mid-sentence fillers, between commas or standing alone (only those that are never real words)
    for m in muletas:
        if m in SO_INICIO:
            continue
        t = re.sub(r"(?i),?\s+" + re.escape(m) + r",?(?=\s)", " ", t)
    # false starts: "I wanted... I wanted" / "I wanted I wanted" (case-insensitive comparison)
    t = _falsos_comecos(t)
    # simple repeated words "the the", "that that"
    t = re.sub(r"\b(\w+)\s+\1\b", lambda m: m.group(1), t, flags=re.I)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    return t or texto


class Modelo:
    """Small MLX model, loaded on first use in its own thread."""

    def __init__(self, nome: str = "mlx-community/Qwen3-4B-4bit"):
        self.nome = nome
        self._lock = threading.Lock()
        self._model = None
        self._tok = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="limpeza")

    def _carregar(self) -> None:
        with self._lock:
            if self._model is None:
                from mlx_lm import load

                t = time.time()
                self._model, self._tok = load(self.nome)
                log.info("cleanup model %s loaded in %.1fs", self.nome, time.time() - t)

    def limpar(self, texto: str) -> str:
        """Synchronous (run in the pool). Returns the original when the guards fail."""
        from mlx_lm import generate

        self._carregar()
        msgs = [{"role": "system", "content": SISTEMA}]
        for u, a in FEW:
            msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
        msgs.append({"role": "user", "content": texto})
        prompt = self._tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        out = generate(self._model, self._tok, prompt=prompt, max_tokens=min(200, 3 * len(texto.split()) + 20), verbose=False).strip()
        return out if guardas_ok(texto, out) else texto


def _palavras(t: str) -> set[str]:
    return set(re.findall(r"\w+", t.lower()))


def guardas_ok(original: str, limpo: str) -> bool:
    if not limpo or "\n" in limpo.strip():
        return False
    if len(limpo) > len(original) + 5:
        return False  # added text
    po, pl = _palavras(original), _palavras(limpo)
    if not pl or len(pl - po) > max(1, len(pl) // 10):
        return False  # invented words
    if len(pl) < 0.5 * len(po):
        return False  # cut too much
    if original.rstrip().endswith("?") != limpo.rstrip().endswith("?"):
        return False  # changed the sentence type
    return True


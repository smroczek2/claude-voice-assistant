"""Falante: rewrites Claude's answer as spoken prose with a local model (LM Studio).

Streams tokens and hands over complete sentences as they close,
so the voice can start speaking before the text is finished.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("voz.rewriter")

SISTEMA = """You are the voice layer of a programming assistant. You receive the assistant's written answer and rewrite it to be said aloud in a real conversation.

Rules:
- English, in a natural tone, like a conversation between colleagues.
- At most {n} short sentences. Get straight to the point; cut details that do not change the decision.
- Never read out code, commands, file paths or URLs; refer to them by description ("the configuration file").
- No lists, headings, markdown symbols or emojis. Prose only.
- If the answer ends with a question or asks for a decision, keep that question as the last sentence, with its meaning unchanged.
- If the answer is already short and conversational, return it almost unchanged.
- Reply only with the text to say, with no introductions."""

_MD = re.compile(r"(```|^\s*[-*+#>|]|\[[^\]]+\]\([^)]+\)|`)", re.M)
_SENT_END = re.compile(r"(?<=[.!?…])\s+")


def precisa_reescrita(text: str, max_chars: int) -> bool:
    return len(text) > max_chars or bool(_MD.search(text))


def limpar_markdown(t: str) -> str:
    t = re.sub(r"```.*?```", " code block. ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)
    # A bare URL is read letter by letter. The screen shows it, so the voice says one word for it.
    t = re.sub(r"<?\bhttps?://[^\s<>]*[^\s<>.,;:!?)\]'\"]>?", "link", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"[*_]{1,3}([^*_\n]+)[*_]{1,3}", r"\1", t)
    t = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"^\s*\|?[\s:|-]+\|\s*$", "", t, flags=re.M)
    t = t.replace("|", ", ").replace(" ", " ")
    return re.sub(r"[ \t]+", " ", t).strip()


def frases(texto: str) -> list[str]:
    return [s.strip() for s in _SENT_END.split(texto) if s.strip()]


class Falante:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg["falante"]
        self.client = httpx.AsyncClient(base_url=self.cfg["base_url"], timeout=self.cfg["timeout_s"])

    async def disponivel(self) -> bool:
        try:
            r = await self.client.get("/models")
            ids = [m["id"] for m in r.json().get("data", [])]
            ok = self.cfg["model"] in ids
            if not ok:
                log.warning("model %r is not loaded in LM Studio (available: %s)", self.cfg["model"], ids)
            return ok
        except Exception as e:  # noqa: BLE001
            log.warning("LM Studio unavailable at %s: %s", self.cfg["base_url"], e)
            return False

    async def falar(self, texto: str) -> AsyncIterator[str]:
        """Generate sentences ready to be spoken. If no rewrite is needed, return the cleaned text split into sentences."""
        if not self.cfg["enabled"] or not precisa_reescrita(texto, self.cfg["passthrough_max_chars"]):
            for s in frases(limpar_markdown(texto)):
                yield s
            return
        try:
            async for s in self._stream(texto):
                yield s
        except Exception as e:  # noqa: BLE001
            log.warning("falante failed (%s); using simple cleanup", e)
            for s in frases(limpar_markdown(texto))[: self.cfg["max_sentences"] + 2]:
                yield s

    async def _stream(self, texto: str) -> AsyncIterator[str]:
        body = {
            "model": self.cfg["model"],
            "temperature": 0.3,
            "max_tokens": 400,
            "reasoning_effort": "low",
            "stream": True,
            "messages": [
                {"role": "system", "content": SISTEMA.format(n=self.cfg["max_sentences"])},
                {"role": "user", "content": texto},
            ],
        }
        buf = ""
        async with self.client.stream("POST", "/chat/completions", json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                delta = json.loads(data)["choices"][0].get("delta", {})
                piece = delta.get("content") or ""
                if not piece:
                    continue
                buf += piece
                parts = _SENT_END.split(buf)
                if len(parts) > 1:
                    for s in parts[:-1]:
                        if s.strip():
                            yield limpar_markdown(s)
                    buf = parts[-1]
        if buf.strip():
            yield limpar_markdown(buf)

    async def aclose(self) -> None:
        await self.client.aclose()


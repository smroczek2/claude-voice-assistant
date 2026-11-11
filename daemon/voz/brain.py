"""Cerebro: Claude Code session via the Agent SDK (runs the embedded CLI, reuses your login).

Streams Claude's text as deltas so the voice can start speaking at the first sentence.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

log = logging.getLogger("voz.brain")

ESTILO_FALADO = """
You are in a voice conversation: the user dictates messages and each sentence you write is spoken aloud as soon as it ends.
Reply in spoken prose, in English, the result first, in short complete sentences.
Let the question set the length: a confirmation takes one or two sentences, and an explanation takes what it needs.
Never use lists, tables, headings, bold text or code blocks in the reply; if you have to show code or
a command, write it to a file and say where it is. Do not read out paths or URLs; describe them.
When you need a decision, end with a single clear question. Do not state decisions the user has not made yet.
Before a long action, say in one short sentence what you are about to do.
""".strip()

Pergunta = Callable[[str], Awaitable[str]]


class Cerebro:
    def __init__(self, cfg: dict[str, Any], cwd: Path, resume: str | None, perguntar: Pergunta):
        c = cfg["claude"]
        self.perguntar = perguntar
        self.session_id: str | None = resume
        self.model = c["model"] or None
        opts = ClaudeAgentOptions(
            cwd=str(cwd),
            resume=resume,
            permission_mode=c["permission_mode"],
            model=self.model,
            max_turns=c["max_turns"] or None,
            setting_sources=["user", "project", "local"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": ESTILO_FALADO},
            can_use_tool=self._can_use_tool,
            include_partial_messages=True,
        )
        self.client = ClaudeSDKClient(options=opts)
        self.busy = False

    async def connect(self) -> None:
        await self.client.connect()

    async def close(self) -> None:
        await self.client.disconnect()

    async def interrupt(self) -> None:
        if self.busy:
            await self.client.interrupt()

    async def turno(self, texto: str) -> AsyncIterator[tuple[str, Any]]:
        """Events: ("delta", str) streaming text · ("block_end", None) end of a text block ·
        ("text", str) complete block · ("tool", str) · ("done", ResultMessage)."""
        self.busy = True
        try:
            await self.client.query(texto)
            async for msg in self.client.receive_response():
                if isinstance(msg, StreamEvent):
                    if msg.parent_tool_use_id:
                        continue
                    ev = msg.event
                    t = ev.get("type")
                    if t == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                        yield ("delta", ev["delta"]["text"])
                    elif t == "content_block_stop":
                        yield ("block_end", None)
                elif isinstance(msg, AssistantMessage):
                    if msg.parent_tool_use_id:
                        continue
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            yield ("text", block.text)
                        elif isinstance(block, ToolUseBlock):
                            yield ("tool", descrever_tool(block.name, block.input or {}))
                elif isinstance(msg, ResultMessage):
                    self.session_id = msg.session_id
                    yield ("done", msg)
        finally:
            self.busy = False

    async def _can_use_tool(self, tool: str, inp: dict, ctx):
        """Permission request: ask by voice and accept yes/no."""
        resposta = await self.perguntar(f"Claude wants to {descrever_tool(tool, inp)}. Allow it?")
        if voice_approves(resposta):
            return PermissionResultAllow(behavior="allow")
        return PermissionResultDeny(behavior="deny", message=f"The user refused by voice: {resposta}")


APPROVE_WORDS = {"yes", "yeah", "yep", "sure", "ok", "okay", "allow", "approve", "approved", "proceed",
                 "sim", "pode", "claro", "avança", "permito", "autorizo"}
APPROVE_PHRASES = {("go", "ahead"), ("do", "it")}
FILLER_WORDS = {"please", "it", "that", "this", "fine", "yes", "now", "thanks", "thank", "you", "claude", "por", "favor"}
APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "`": "'", "-": " ", "\u2014": " ", "\u2013": " "})


def voice_approves(answer: str) -> bool:
    """True only when the whole answer is a short yes: approval words, approval phrases and filler.

    Anything else refuses: a question, a denial, extra content, a long answer or an empty one.
    """
    text = answer.lower().translate(APOSTROPHES)
    if "?" in text or "n't" in text:
        return False
    words = re.sub(r"[.!,;:]", " ", text).split()
    if not words or len(words) > 6:
        return False
    approved, i = False, 0
    while i < len(words):
        if tuple(words[i:i + 2]) in APPROVE_PHRASES:
            approved, i = True, i + 2
        elif words[i] in APPROVE_WORDS:
            approved, i = True, i + 1
        elif words[i] in FILLER_WORDS:
            i += 1
        else:
            return False
    return approved


def descrever_tool(n: str, i: dict) -> str:
    if n == "Bash":
        cmd = str(i.get("command", ""))
        return f"run the command {cmd[:80]}" + ("…" if len(cmd) > 80 else "")
    if n in ("Read", "Glob", "Grep"):
        alvo = i.get("file_path") or i.get("pattern") or i.get("path") or ""
        return f"read {Path(str(alvo)).name or alvo}".strip()
    if n in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"change the file {Path(str(i.get('file_path', ''))).name}"
    if n == "WebFetch":
        return f"look up {i.get('url', '')}"
    if n == "WebSearch":
        return f"search the web for {i.get('query', '')}"
    if n in ("Agent", "Task"):
        return f"launch a subagent: {i.get('description', '')}"
    return f"use the {n} tool"


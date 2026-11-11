"""Meeting mode: speaker identification by voice signature and timestamped minutes.

Speakers: each sentence produces a signature (sherpa-onnx, 3D-Speaker ERes2Net, 512 dims, ~40 ms).
Similar signatures (cosine >= threshold) are the same person; each speaker's centroid is refined
as more speech arrives. New voices get "Orador N" until someone says "eu sou o João" / "este é o João"
(Portuguese self-introductions), or until renamed in the panel. The minutes are a markdown file with one line per turn.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

import httpx
import numpy as np

from .models import ModelError, sha256
from .private_files import open_private, private_dir

log = logging.getLogger("voz.meeting")

MODELO_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
MODELO = Path.home() / ".voz" / "models" / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
# Pinned on 2026-11-07: the release publishes no digest, so the engine checks this hash on every load.
MODELO_SHA256 = "1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b"
ACTAS = Path.home() / ".voz" / "reunioes"

# "eu sou o João", "sou a Maria", "chamo-me Rui", "este é o Pedro", "aqui é a Ana"
# Only the introduction phrase ignores case: the name must start with a capital letter, otherwise "eu sou contra isso" would yield a name.
RE_NOME = re.compile(
    r"\b(?i:eu sou|sou|chamo-me|o meu nome é|aqui (?:é|fala)|este é|esta é|fala)\s+(?i:[oa]\s+)?"
    r"([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÁÉÍÓÚÂÊÔÃÕÇáéíóúâêôãõç-]{1,30}(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wáéíóúâêôãõç-]{1,30})?)\b"
)


def garantir_modelo() -> Path:
    """The speaker model, downloaded once and hash-checked on every load."""
    if MODELO.is_file():
        if sha256(MODELO) == MODELO_SHA256:
            return MODELO
        MODELO.unlink()
        log.warning("speaker model did not match its pinned hash; downloading it again")
    MODELO.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading speaker model (40 MB)…")
    partial = MODELO.with_suffix(".part")
    with httpx.stream("GET", MODELO_URL, timeout=600, follow_redirects=True) as r:
        r.raise_for_status()
        with open(partial, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    if sha256(partial) != MODELO_SHA256:
        partial.unlink()
        raise ModelError(f"the speaker model from {MODELO_URL} does not match its pinned SHA-256; it was deleted")
    partial.replace(MODELO)
    return MODELO


class Oradores:
    def __init__(self, limiar: float = 0.7):
        import sherpa_onnx

        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(garantir_modelo()), num_threads=2)
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.limiar = limiar
        self.lista: list[dict] = []  # {name, centroid, n}
        self.assinaturas: list[tuple[int, np.ndarray, str]] = []  # (turn index, signature, assigned name)
        self.k: int | None = None    # expected number of speakers, if the user says so

    def assinatura(self, audio: np.ndarray, sr: int = 16000) -> np.ndarray | None:
        if len(audio) < sr * 0.6:
            return None
        st = self._ex.create_stream()
        st.accept_waveform(sr, audio.astype(np.float32))
        st.input_finished()
        e = np.array(self._ex.compute(st), dtype=np.float32)
        n = np.linalg.norm(e)
        return e / n if n > 0 else None

    def identificar(self, audio: np.ndarray, sr: int = 16000, idx: int | None = None) -> tuple[str, float, bool]:
        """Returns (name, similarity, is_new). If no signature is possible, returns the last speaker or 'Orador 1'."""
        e = self.assinatura(audio, sr)
        if e is None:
            nome = self.lista[-1]["name"] if self.lista else self._novo(None)
            return nome, 0.0, False
        melhor, sim = None, -1.0
        for o in self.lista:
            s = float(e @ o["centroid"])
            if s > sim:
                melhor, sim = o, s
        # with k known and k speakers already present, the turn always goes to the most similar one
        aceita = melhor is not None and (sim >= self.limiar or (self.k is not None and len(self.lista) >= self.k))
        if aceita:
            melhor["centroid"] = melhor["centroid"] * melhor["n"] + e
            melhor["n"] += 1
            melhor["centroid"] /= np.linalg.norm(melhor["centroid"])
            nome, novo = melhor["name"], False
        else:
            nome, novo = self._novo(e), True
        if idx is not None:
            self.assinaturas.append((idx, e, nome))
        return nome, sim, novo

    def reagrupar(self, k: int | None = None) -> dict[int, str]:
        """Regroup all turns that have a signature (agglomerative, average linkage, cosine).
        With k, stop at exactly k groups; without k, cut at the threshold. Keeps the names already given
        (the most frequent name in each group). Returns {turn index: name}."""
        k = k or self.k
        if len(self.assinaturas) < 2:
            return {}
        idxs = [i for i, _, _ in self.assinaturas]
        E = np.stack([e for _, e, _ in self.assinaturas])
        grupos: list[list[int]] = [[i] for i in range(len(E))]
        cent = [E[i].copy() for i in range(len(E))]
        while len(grupos) > 1:
            melhor, a_, b_ = -1.0, -1, -1
            for a in range(len(grupos)):
                for b in range(a + 1, len(grupos)):
                    sim = float(cent[a] @ cent[b] / (np.linalg.norm(cent[a]) * np.linalg.norm(cent[b]) + 1e-9))
                    if sim > melhor:
                        melhor, a_, b_ = sim, a, b
            if k is not None:
                if len(grupos) <= k:
                    break
            elif melhor < self.limiar - 0.05:  # average linkage is stricter: small margin
                break
            grupos[a_] += grupos[b_]; cent[a_] = E[grupos[a_]].mean(axis=0)
            del grupos[b_]; del cent[b_]
        # names: the most frequent among those already assigned to each group; prefer names given by the person
        nomes_antigos = [n for _, _, n in self.assinaturas]
        usados: set[str] = set()
        nova_lista: list[dict] = []
        mapa: dict[int, str] = {}
        ordem = sorted(range(len(grupos)), key=lambda g: min(grupos[g]))  # in order of appearance
        for g in ordem:
            contagem: dict[str, int] = {}
            for m in grupos[g]:
                contagem[nomes_antigos[m]] = contagem.get(nomes_antigos[m], 0) + 1
            candidatos = sorted(contagem, key=lambda n: (n.startswith("Orador"), -contagem[n]))
            nome = next((c for c in candidatos if c not in usados), None) or f"Orador {len(nova_lista) + 1}"
            usados.add(nome)
            c = E[grupos[g]].mean(axis=0); c /= np.linalg.norm(c)
            nova_lista.append({"name": nome, "centroid": c, "n": len(grupos[g])})
            for m in grupos[g]:
                mapa[idxs[m]] = nome
        self.lista = nova_lista
        self.assinaturas = [(i, e, mapa[i]) for i, e, _ in self.assinaturas]
        return mapa

    def _novo(self, e: np.ndarray | None) -> str:
        nome = f"Orador {len(self.lista) + 1}"
        self.lista.append({"name": nome, "centroid": e if e is not None else np.zeros(self._ex.dim, dtype=np.float32), "n": 1 if e is not None else 0})
        return nome

    def renomear(self, de: str, para: str) -> bool:
        """Rename; if the new name already exists, merge the two speakers."""
        para = para.strip()
        if not para:
            return False
        alvo = next((o for o in self.lista if o["name"].lower() == de.lower()), None)
        if alvo is None:
            return False
        existente = next((o for o in self.lista if o["name"].lower() == para.lower() and o is not alvo), None)
        if existente is not None:
            tot = existente["n"] + alvo["n"]
            existente["centroid"] = existente["centroid"] * existente["n"] + alvo["centroid"] * alvo["n"]
            existente["centroid"] /= np.linalg.norm(existente["centroid"]) + 1e-9
            existente["n"] = tot
            self.lista.remove(alvo)
            para = existente["name"]
        else:
            alvo["name"] = para
        self.assinaturas = [(i, e, para if n.lower() == de.lower() else n) for i, e, n in self.assinaturas]
        return True

    def nomes(self) -> list[str]:
        return [o["name"] for o in self.lista]


class Acta:
    def __init__(self, titulo: str):
        private_dir(ACTAS)
        self.inicio = datetime.now()
        slug = re.sub(r"[^\w-]+", "-", titulo.strip().lower()).strip("-") or "meeting"
        self.path = ACTAS / f"{self.inicio:%Y-%m-%d-%H%M}-{slug}.md"
        self.titulo = titulo.strip() or "Meeting"
        self.entradas: list[dict] = []
        self._escrever()

    def adicionar(self, orador: str, texto: str) -> dict:
        e = {"t": datetime.now().strftime("%H:%M:%S"), "who": orador, "text": texto.strip()}
        self.entradas.append(e)
        with open_private(self.path, "a") as f:
            f.write(f"- **{e['t']}** — **{e['who']}**: {e['text']}\n")
        return e

    def renomear(self, de: str, para: str) -> None:
        for e in self.entradas:
            if e["who"] == de:
                e["who"] = para
        self._escrever()

    def reatribuir(self, mapa: dict[int, str]) -> int:
        """Apply {index: name} to the entries; returns how many changed."""
        mudou = 0
        for i, e in enumerate(self.entradas):
            novo = mapa.get(i)
            if novo and e["who"] != novo:
                e["who"] = novo; mudou += 1
        if mudou:
            self._escrever()
        return mudou

    def _escrever(self) -> None:
        with open_private(self.path, "w") as f:
            f.write(f"# Minutes — {self.titulo}\n\n")
            f.write(f"Start: {self.inicio:%Y-%m-%d %H:%M}\n\n## Turns\n\n")
            for e in self.entradas:
                f.write(f"- **{e['t']}** — **{e['who']}**: {e['text']}\n")

    def fechar(self) -> Path:
        with open_private(self.path, "a") as f:
            f.write(f"\nEnd: {datetime.now():%H:%M}\n")
        return self.path


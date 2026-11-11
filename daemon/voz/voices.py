"""Local neural voices: Chatterbox Turbo, English Qwen3/Kokoro, and reference clones.

Apple voice helpers are kept for the voz CLI; they are outside this catalog.
ids: turbo:default · mlx:voice:<preset> · mlx:ref:<file.wav> · kokoro:<voice>
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

from huggingface_hub.constants import HF_HUB_CACHE

log = logging.getLogger("voz.voices")

VOZ_DIR = Path.home() / ".voz"
QWEN_LANGS = {"zh", "en", "de", "it", "pt", "es", "ja", "ko", "fr", "ru"}
QWEN_PRESETS = ["Ryan", "Aiden", "Serena", "Vivian", "Dylan", "Eric", "Uncle_Fu", "Ono_Anna", "Sohee"]
KOKORO_CACHE = Path(HF_HUB_CACHE) / "models--mlx-community--Kokoro-82M-bf16"
KOKORO_PRESETS = [
    ("af_heart", "Heart", "en_US"),
    ("af_bella", "Bella", "en_US"),
    ("am_michael", "Michael", "en_US"),
    ("bf_emma", "Emma", "en_GB"),
    ("bm_george", "George", "en_GB"),
]

# languages Whisper transcribes that we offer as a choice (code → name)
LINGUAS = {
    "pt_PT": "Português (Portugal)", "pt_BR": "Português (Brasil)", "en": "English", "es": "Español",
    "fr": "Français", "de": "Deutsch", "it": "Italiano", "nl": "Nederlands", "pl": "Polski", "ru": "Русский",
    "uk": "Українська", "cs": "Čeština", "sv": "Svenska", "da": "Dansk", "no": "Norsk", "fi": "Suomi",
    "el": "Ελληνικά", "tr": "Türkçe", "ro": "Română", "hu": "Magyar", "ca": "Català", "zh": "中文",
    "ja": "日本語", "ko": "한국어", "ar": "العربية", "hi": "हिन्दी", "id": "Bahasa Indonesia", "vi": "Tiếng Việt",
}


def familia(code: str) -> str:
    """'pt_PT' → 'pt'; 'en' → 'en'."""
    return (code or "").replace("-", "_").split("_")[0].lower()


def piper_bin() -> str:
    cand = Path(sys.executable).parent / "piper"
    return str(cand) if cand.exists() else "piper"


# ---------- Apple ----------
def vozes_say() -> list[dict]:
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return []
    res = []
    for line in out.splitlines():
        m = re.match(r"^(.*?)\s+([a-z]{2}_[A-Z]{2})\s", line)  # long names end up one space away from the language
        if not m:
            continue
        nome, loc = m.group(1).strip(), m.group(2)
        # "Joana (Enhanced)" → "Joana Enhanced (Apple)"; "Joana (Premium)" → "Joana Premium (Apple)"
        base, _, var = nome.partition(" (")
        var = var.rstrip(")")
        qual = {"Enhanced": "Enhanced", "Premium": "Premium"}.get(var, var)
        label = f"{base} {qual} (Apple)" if qual else f"{base} (Apple)"
        res.append({"id": f"say:{nome}", "label": label, "lang": loc, "source": "apple", "installed": True, "_base": base, "_var": var})
    # with the Enhanced/Premium version installed, the compact one of the same name is no longer of interest
    melhores = {(r["_base"], r["lang"]) for r in res if r["_var"] in ("Enhanced", "Premium")}
    res = [r for r in res if r["_var"] or (r["_base"], r["lang"]) not in melhores]
    for r in res:
        r.pop("_base"); r.pop("_var")
    return res


# ---------- Qwen3 ----------
def vozes_qwen(refs: list[Path]) -> list[dict]:
    res = []
    gravada = VOZ_DIR / "ref.wav"
    for r in refs:
        nome = "Your voice" if r == gravada else r.stem
        res.append({"id": f"mlx:ref:{r}", "label": f"{nome} (neural clone, Qwen3)", "lang": "*", "source": "qwen-clone", "installed": True})
    for v in QWEN_PRESETS:
        native = 'English' if v in {'Ryan', 'Aiden'} else 'multilingual'
        res.append({"id": f"mlx:voice:{v}", "label": f"{v.replace('_', ' ')} (Qwen3, {native})", "lang": "en", "source": "qwen", "installed": True})
    return res


def vozes_kokoro() -> list[dict]:
    """Inspect local voice packs only; catalog rendering never downloads models."""
    return [
        {
            "id": f"kokoro:{voice}",
            "label": f"{name} (Kokoro, {'British' if lang == 'en_GB' else 'American'} English)",
            "lang": lang,
            "source": "kokoro",
            "installed": any(path.is_file() for path in KOKORO_CACHE.glob(f"snapshots/*/voices/{voice}.safetensors")),
        }
        for voice, name, lang in KOKORO_PRESETS
    ]


# ---------- search ----------
def procurar(lang: str | None, query: str | None, refs: list[Path]) -> list[dict]:
    fam = familia(lang) if lang else ""
    q = (query or "").strip().lower()
    turbo = {"id": "turbo:default", "label": "Chatterbox Turbo (English)", "source": "turbo", "lang": "en", "installed": True}
    clones = [v for v in vozes_qwen(refs) if v["source"] == "qwen-clone"]
    george = [v for v in vozes_kokoro() if v["id"] == "kokoro:bm_george"]
    todas = [turbo] + clones + george
    out = []
    for v in todas:
        vl = v["lang"]
        if fam:
            if vl == "*":
                if fam not in QWEN_LANGS:
                    continue
            elif lang and "_" in lang and vl != lang and familia(vl) == fam:
                # a specific region was requested: show that region first, the others after
                v = {**v, "_rank": 1}
            elif familia(vl) != fam:
                continue
        if q and q not in v["label"].lower() and q not in v["id"].lower() and q not in vl.lower() and q not in LINGUAS.get(vl, "").lower():
            continue
        out.append(v)
    ordem = {"turbo": 0, "qwen-clone": 1, "qwen": 2, "kokoro": 3}
    out.sort(key=lambda v: (
        v.get("_rank", 0), 0 if v["installed"] else 1,
        ordem.get(v["source"], 9), v["label"],
    ))
    for v in out:
        v.pop("_rank", None)
    return out[:40]


# Apple "novelty" voices: they exist for all languages but are not suited to conversation
NOVELTY = {
    "Albert", "Bad News", "Bahh", "Bells", "Boing", "Bubbles", "Cellos", "Deranged", "Good News", "Jester", "Organ",
    "Superstar", "Trinoids", "Whisper", "Wobble", "Zarvox", "Eddy", "Flo", "Grandma", "Grandpa", "Reed", "Rocko",
    "Sandy", "Shelley", "Junior", "Ralph", "Kathy", "Fred",
}


def _novelty(nome: str) -> bool:
    return nome.split(" (")[0] in NOVELTY


def voz_say_para(lang: str, instaladas: list[str] | None = None) -> str | None:
    """First say voice for the language (prefers Premium/Enhanced and the requested region; avoids novelty voices)."""
    cands = [v for v in vozes_say() if familia(v["lang"]) == familia(lang)]
    if not cands:
        return None
    cands.sort(key=lambda v: (
        1 if _novelty(v["id"].split(":", 1)[1]) else 0,
        0 if v["lang"] == lang else 1,
        0 if "Premium" in v["id"] else 1 if "Enhanced" in v["id"] else 2,
        v["id"],
    ))
    return cands[0]["id"].split(":", 1)[1]


# Initial Whisper prompt per language: steers toward the right spelling and punctuation (pt-PT vs pt-BR, etc.).
PROMPTS = {
    "pt_PT": "Olá, tudo bem? Vou enviar o relatório ainda hoje. Está bem, combinado. Vamos rever o projecto, a autenticação e o deploy.",
    "pt_BR": "Olá, tudo bem? Vou enviar o relatório ainda hoje. Tá bom, combinado. Vamos revisar o projeto, a autenticação e o deploy.",
    "en": "Hi, how are you? I'll send the report today. Okay, agreed. Let's review the project, the authentication and the deployment.",
    "es": "Hola, ¿qué tal? Enviaré el informe hoy. Vale, de acuerdo. Revisemos el proyecto, la autenticación y el despliegue.",
    "fr": "Bonjour, ça va ? J'enverrai le rapport aujourd'hui. D'accord. Revoyons le projet, l'authentification et le déploiement.",
    "de": "Hallo, wie geht's? Ich schicke den Bericht heute. Okay, abgemacht. Lass uns das Projekt, die Authentifizierung und das Deployment durchgehen.",
    "it": "Ciao, come va? Invierò il rapporto oggi. Va bene, d'accordo. Rivediamo il progetto, l'autenticazione e il deploy.",
}


def prompt_para(lang: str) -> str:
    return PROMPTS.get(lang) or PROMPTS.get(familia(lang), "")


SUBSTITUICOES = VOZ_DIR / "substituicoes.csv"


def substituicoes() -> list[tuple[str, str]]:
    """`from;to` pairs, one per line, in ~/.voz/substituicoes.csv (ignores empty lines and #)."""
    if not SUBSTITUICOES.is_file():
        return []
    pares = []
    for line in SUBSTITUICOES.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ";" not in line:
            continue
        de, _, para = line.partition(";")
        if de.strip():
            pares.append((de.strip(), para.strip()))
    return pares


def aplicar_substituicoes(texto: str) -> str:
    for de, para in substituicoes():
        texto = re.sub(r"(?i)\b" + re.escape(de) + r"\b", para, texto)
    return texto


"""Configuration loading (config.toml) with defaults."""

from __future__ import annotations

import tomllib
from copy import deepcopy
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "audio": {"backend": "native", "input_device": "", "sample_rate": 16000},
    "vad": {
        "mode": "auto",
        "start_threshold": 0.45,
        "end_threshold": 0.3,
        "min_speech_ms": 100,
        "noise_margin_db": 8.0,   # speech must be this many dB above the measured room level
        "min_level_db": -55.0,    # and never quieter than this absolute level (dBFS)
        "silence_ms": 450,
        "turn_gap_ms": 2500,
        "pre_roll_ms": 450,
        "max_utterance_s": 60,
        "barge_in": True,
        "barge_in_threshold": 0.85,
        "barge_in_min_ms": 350,
        "interrupt_min_s": 1.0,
        "interrupt_min_words": 3,
    },
    "stt": {
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "en",
        "min_duration_s": 0.3,
        "initial_prompt": "",
        "live": True,
        "live_model": "mlx-community/parakeet-tdt-0.6b-v3",
    },
    "falante": {
        "enabled": False,
        "base_url": "http://localhost:1234/v1",
        "model": "falante",
        "max_sentences": 4,
        "passthrough_max_chars": 350,
        "timeout_s": 60,
    },
    "tts": {
        "backend": "turbo",
        "voice": "Ryan",
        "prefer_premium": True,
        "rate": 185,
        "speed": 1.0,
        "expression": "restrained",
        "turbo_model": "mlx-community/chatterbox-turbo-8bit",
        "kokoro_model": "mlx-community/Kokoro-82M-bf16",
        "kokoro_voice": "bm_george",
        "lang": "en",
        "mlx_model": "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
        "mlx_ref_audio": "",
        "mlx_ref_text": "",
        "mlx_voice": "Ryan",
        "mlx_lang": "en",
    },
    "claude": {"permission_mode": "default", "model": "sonnet", "max_turns": 0},
    "cleanup": {"mode": "regras", "model": "mlx-community/Qwen3-4B-4bit"},
    "keywords": {"dynamic": True, "model": "mlx-community/Qwen3-4B-4bit", "max_words": 6},
    "meeting": {"input_device": "", "threshold": 0.7, "silence_ms": 900},
    "ui": {"enabled": True, "port": 8766, "ack_sound": True},
}


def _merge(base: dict, extra: dict) -> dict:
    out = deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_layer(cfg: dict[str, Any], path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        layer = tomllib.load(f)
    # relative paths in the config resolve against that file's folder
    ref = (layer.get("tts") or {}).get("mlx_ref_audio") or ""
    if ref and not ref.startswith(("/", "~")):
        layer["tts"]["mlx_ref_audio"] = str(path.parent / ref)
    return _merge(cfg, layer)


def user_config_path() -> Path:
    """The user's own overrides. Engine updates never write this file."""
    return Path.home() / ".voz" / "config.toml"


def load_config(explicit: Path | None = None) -> dict[str, Any]:
    """Engine defaults, then the user's overrides.

    Layer 1 is the first file found of: the explicit path, ./config.toml, the
    engine's daemon/config.toml. Layer 2 is ~/.voz/config.toml, applied on top,
    so an engine update cannot hide or discard the user's own tuning.
    """
    candidates = [
        explicit,
        Path.cwd() / "config.toml",
        Path(__file__).resolve().parent.parent / "config.toml",
    ]
    cfg = deepcopy(DEFAULTS)
    user = user_config_path()
    for p in candidates:
        if p and p.is_file() and p.resolve() != user.resolve():
            cfg = _read_layer(cfg, p)
            cfg["_path"] = str(p)
            break
    if user.is_file():
        cfg = _read_layer(cfg, user)
        cfg["_user_path"] = str(user)
        cfg.setdefault("_path", str(user))
    # Existing recordings remain catalog choices; only an explicit selection
    # may replace a preset or the bundled Turbo voice with a reference clone.
    return cfg

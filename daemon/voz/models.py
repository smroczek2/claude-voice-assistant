"""Pinned model downloads: each Hugging Face repository is fixed to one commit and every file is hash-checked.

`daemon/models.lock.json` holds, for each repository, the commit and the SHA-256 of
each file the engine loads. `prepare()` downloads exactly those files at that
commit, checks every hash, and points the cache's `refs/main` at the commit. The
daemon then runs with the Hub offline, so the loaders can only open the verified
snapshot. To change a model, run `scripts/pin_models.py` and commit the new lock.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

LOCK_PATH = Path(__file__).resolve().parents[1] / "models.lock.json"


class ModelError(RuntimeError):
    pass


def load_pins(path: Path = LOCK_PATH) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as e:
        raise ModelError(f"{path} is missing; the engine cannot check its models") from e
    models = data.get("models")
    if data.get("version") != 1 or not isinstance(models, dict) or not models:
        raise ModelError(f"{path} is not a version 1 model lock")
    return models


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def configured_models(cfg: dict) -> set[str]:
    """Every Hugging Face repository the configuration can load."""
    stt, tts = cfg.get("stt", {}), cfg.get("tts", {})
    names = {stt.get("model"), stt.get("live_model"), tts.get("turbo_model"), tts.get("kokoro_model"), tts.get("mlx_model"),
             cfg.get("cleanup", {}).get("model"), cfg.get("keywords", {}).get("model")}
    return {n for n in names if isinstance(n, str) and n and not Path(n).expanduser().is_absolute()}


def check_configured(cfg: dict, pins: dict[str, dict] | None = None) -> None:
    """Fail fast when the configuration names a model that has no pin."""
    pins = load_pins() if pins is None else pins
    unpinned = sorted(configured_models(cfg) - set(pins))
    if unpinned:
        raise ModelError(
            "these models have no pinned revision: " + ", ".join(unpinned)
            + f". Add them with scripts/pin_models.py (it updates {LOCK_PATH.name}), or use a pinned model.")


def repo_dir(cache: Path, repo: str) -> Path:
    return cache / ("models--" + repo.replace("/", "--"))


def verify_snapshot(cache: Path, repo: str, pin: dict) -> list[str]:
    """Files of the pinned snapshot that are missing or do not match their hash."""
    snapshot = repo_dir(cache, repo) / "snapshots" / pin["revision"]
    bad = []
    for name, digest in pin["files"].items():
        f = snapshot / name
        if not f.is_file() or sha256(f) != digest:
            bad.append(name)
    return bad


def prepare(repos: Iterable[str], pins: dict[str, dict] | None = None, cache: Path | None = None) -> None:
    """Download each pinned file at its commit, check its hash, then point refs/main at the commit."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.constants import HF_HUB_CACHE

    pins = load_pins() if pins is None else pins
    cache = Path(cache or HF_HUB_CACHE)
    for repo in repos:
        pin = pins[repo]
        for name, digest in pin["files"].items():
            path = Path(hf_hub_download(repo, name, revision=pin["revision"], cache_dir=str(cache)))
            if sha256(path) != digest:
                path.unlink(missing_ok=True)
                raise ModelError(f"{repo}/{name} does not match its pinned SHA-256; the bad download was deleted. Run setup again.")
        ref = repo_dir(cache, repo) / "refs" / "main"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(pin["revision"])


def go_offline() -> None:
    """From here on the Hub is offline: loaders open only what is in the cache."""
    import os

    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        from huggingface_hub import constants
    except ImportError:
        return
    constants.HF_HUB_OFFLINE = True

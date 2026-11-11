"""Maintainer tool: write daemon/models.lock.json, the pinned model revisions and file hashes.

Run from daemon/:  uv run --frozen python ../scripts/pin_models.py
It pins each repository to the commit already in the local Hugging Face cache
(or to the current `main` when the cache has none), lists the files the engine
loads, and records the SHA-256 of each file from the Hub's LFS metadata. Small
files that are not in LFS are downloaded at the pinned commit and hashed. Every
file already in the local cache is checked against the recorded hash.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import sys
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.constants import HF_HUB_CACHE

LOCK = Path(__file__).resolve().parents[1] / "daemon" / "models.lock.json"
MLX_AUDIO_PATTERNS = ["*.json", "*.safetensors", "*.py", "*.model", "*.tiktoken", "*.txt", "*.jinja", "*.jsonl", "*.yaml", "*.npz", "*.pth"]
# repo -> (required by setup, file patterns the loaders fetch)
MODELS = {
    "mlx-community/whisper-large-v3-turbo": (True, ["*"]),
    "mlx-community/parakeet-tdt-0.6b-v3": (True, ["config.json", "model.safetensors"]),
    "mlx-community/chatterbox-turbo-8bit": (True, MLX_AUDIO_PATTERNS),
    "mlx-community/Kokoro-82M-bf16": (True, ["config.json", "kokoro-v1_0.safetensors", "voices/bm_george.safetensors"]),
    "mlx-community/Qwen3-4B-4bit": (True, ["*.json", "*.safetensors", "*.py", "*.model", "*.tiktoken", "*.txt", "*.jinja", "*.jsonl"]),
    "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit": (False, MLX_AUDIO_PATTERNS),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def local_revision(repo: str) -> str | None:
    ref = Path(HF_HUB_CACHE) / ("models--" + repo.replace("/", "--")) / "refs" / "main"
    return ref.read_text().strip() if ref.is_file() else None


def pin(api: HfApi, repo: str, required: bool, patterns: list[str]) -> dict:
    revision = local_revision(repo) or api.model_info(repo).sha
    files = {}
    for entry in api.list_repo_tree(repo, revision=revision, recursive=True, expand=True):
        path = getattr(entry, "path", "")
        if not hasattr(entry, "size") or not any(fnmatch.fnmatch(path, p) for p in patterns):
            continue
        if path.startswith(".") or path.endswith(".md"):
            continue
        lfs = getattr(entry, "lfs", None)
        digest = lfs.sha256 if lfs else sha256(Path(hf_hub_download(repo, path, revision=revision)))
        files[path] = digest
        cached = Path(HF_HUB_CACHE) / ("models--" + repo.replace("/", "--")) / "snapshots" / revision / path
        if cached.is_file() and sha256(cached) != digest:
            sys.exit(f"{repo}/{path}: the cached file does not match the Hub's hash. Delete it and run again.")
    if not files:
        sys.exit(f"{repo}: no files matched {patterns} at {revision}")
    return {"revision": revision, "required": required, "files": dict(sorted(files.items()))}


def main() -> None:
    api = HfApi()
    pins = {repo: pin(api, repo, *spec) for repo, spec in MODELS.items()}
    LOCK.write_text(json.dumps({"version": 1, "models": pins}, indent=2) + "\n")
    print(f"wrote {LOCK} with {len(pins)} models")


if __name__ == "__main__":
    main()

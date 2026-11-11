import hashlib
import json

import pytest

from voz.config import DEFAULTS
from voz.models import ModelError, check_configured, configured_models, load_pins, prepare, verify_snapshot

HEX40 = "0123456789abcdef0123456789abcdef01234567"


def test_lock_pins_every_default_model_to_a_commit_with_sha256_hashes():
    pins = load_pins()
    assert configured_models(DEFAULTS) <= set(pins)
    for repo, pin in pins.items():
        assert len(pin["revision"]) == 40 and int(pin["revision"], 16) >= 0, repo
        assert pin["files"], repo
        for name, digest in pin["files"].items():
            assert len(digest) == 64 and int(digest, 16) >= 0, (repo, name)
    check_configured(DEFAULTS, pins)


def test_unpinned_model_fails_fast_with_the_fix():
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["stt"]["model"] = "someone/unpinned-model"
    with pytest.raises(ModelError, match="pin_models.py"):
        check_configured(cfg)


def test_local_model_paths_need_no_pin(tmp_path):
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["stt"]["model"] = str(tmp_path)
    check_configured(cfg)


def test_bad_lock_file_is_refused(tmp_path):
    bad = tmp_path / "lock.json"
    bad.write_text('{"version": 2, "models": {}}')
    with pytest.raises(ModelError):
        load_pins(bad)
    with pytest.raises(ModelError, match="missing"):
        load_pins(tmp_path / "absent.json")


def _fake_download(cache, content):
    def download(repo, name, revision, cache_dir):
        path = cache / ("models--" + repo.replace("/", "--")) / "snapshots" / revision / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return str(path)
    return download


def test_prepare_checks_hashes_and_points_main_at_the_pin(tmp_path, monkeypatch):
    good = b"weights"
    pins = {"org/model": {"revision": HEX40, "required": True, "files": {"w.bin": hashlib.sha256(good).hexdigest()}}}
    monkeypatch.setattr("huggingface_hub.hf_hub_download", _fake_download(tmp_path, good))
    prepare(["org/model"], pins, tmp_path)
    assert (tmp_path / "models--org--model" / "refs" / "main").read_text() == HEX40
    assert verify_snapshot(tmp_path, "org/model", pins["org/model"]) == []


def test_prepare_deletes_a_tampered_download(tmp_path, monkeypatch):
    pins = {"org/model": {"revision": HEX40, "required": True, "files": {"w.bin": hashlib.sha256(b"weights").hexdigest()}}}
    monkeypatch.setattr("huggingface_hub.hf_hub_download", _fake_download(tmp_path, b"tampered"))
    with pytest.raises(ModelError, match="SHA-256"):
        prepare(["org/model"], pins, tmp_path)
    snapshot = tmp_path / "models--org--model" / "snapshots" / HEX40
    assert not (snapshot / "w.bin").exists()
    assert not (tmp_path / "models--org--model" / "refs" / "main").exists()
    assert verify_snapshot(tmp_path, "org/model", pins["org/model"]) == ["w.bin"]

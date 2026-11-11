import hashlib
import stat

import pytest

from voz import meeting
from voz.models import ModelError
from voz.private_files import open_private, private_dir


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_open_private_makes_new_and_old_files_owner_only(tmp_path):
    new = tmp_path / "new.txt"
    with open_private(new) as f:
        f.write("secret")
    old = tmp_path / "old.txt"
    old.write_text("x")
    old.chmod(0o644)
    with open_private(old, "a") as f:
        f.write("y")
    assert mode(new) == 0o600 and mode(old) == 0o600
    assert old.read_text() == "xy"
    with pytest.raises(ValueError):
        open_private(new, "r")


def test_private_dir_tightens_an_existing_folder(tmp_path):
    folder = tmp_path / "a" / "b"
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    private_dir(folder)
    assert mode(folder) == 0o700


def test_meeting_minutes_are_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(meeting, "ACTAS", tmp_path / "minutes")
    acta = meeting.Acta("Planning")
    acta.adicionar("Alex", "First point.")
    path = acta.fechar()
    assert mode(path) == 0o600 and mode(tmp_path / "minutes") == 0o700
    assert "First point." in path.read_text()


class _Stream:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self, _n):
        yield self.body


def test_speaker_model_download_is_hash_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(meeting, "MODELO", tmp_path / "models" / "speaker.onnx")
    monkeypatch.setattr(meeting, "MODELO_SHA256", hashlib.sha256(b"good").hexdigest())
    monkeypatch.setattr(meeting.httpx, "stream", lambda *a, **k: _Stream(b"tampered"))
    with pytest.raises(ModelError, match="SHA-256"):
        meeting.garantir_modelo()
    assert not list((tmp_path / "models").iterdir())
    monkeypatch.setattr(meeting.httpx, "stream", lambda *a, **k: _Stream(b"good"))
    assert meeting.garantir_modelo().read_bytes() == b"good"
    meeting.MODELO.write_bytes(b"changed on disk")
    assert meeting.garantir_modelo().read_bytes() == b"good"

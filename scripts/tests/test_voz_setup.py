"""Setup, profile and migration run in a temporary HOME and never touch the real one."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voz_setup  # noqa: E402

ENGINE = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home"
    folder.mkdir()
    monkeypatch.setenv("HOME", str(folder))
    monkeypatch.delenv("VOZ_STATE_DIR", raising=False)
    return folder


def test_new_profile_has_the_speaking_rules_and_a_working_shim(home):
    profile = home / "voice-orchestrator"
    voz_setup.create_profile(profile, "generic", "Alex")
    assert (profile / "context" / "speaking.md").read_text() == (ENGINE / "profile-template/context/speaking.md").read_text()
    assert not (profile / "context" / "workplace.md").exists()
    assert json.loads((home / ".claude/voice-orchestrator/config.json").read_text())["user_name"] == "Alex"
    run = subprocess.run([sys.executable, "scripts/ledger.py", "list"], cwd=profile, capture_output=True, text=True,
                         env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert run.returncode == 0, run.stderr
    assert voz_setup.check_profile(profile).state == "complete"


def test_profile_update_never_overwrites_user_files(home):
    profile = home / "voice-orchestrator"
    voz_setup.create_profile(profile, "generic", None)
    (profile / "CLAUDE.md").write_text("my own instructions\n")
    (profile / "context" / "speaking.md").unlink()
    actions = voz_setup.create_profile(profile, "generic", None)
    assert (profile / "CLAUDE.md").read_text() == "my own instructions\n"
    assert (profile / "context" / "speaking.md").is_file()
    assert any("keep your" in a and "CLAUDE.md" in a for a in actions)
    assert (profile / ".voz" / "template" / "CLAUDE.md").read_text() == (ENGINE / "profile-template/CLAUDE.md").read_text()


def test_flavor_overlay_adds_only_its_files(home):
    flavors = voz_setup.flavors()
    assert flavors[0] == "generic"
    for flavor in flavors[1:]:
        profile = home / flavor
        voz_setup.create_profile(profile, flavor, None)
        assert (profile / "context" / "workplace.md").is_file()
        assert (profile / "context" / "speaking.md").is_file()


def test_refuses_unrelated_folder_and_engine_folder(home):
    other = home / "Documents"
    other.mkdir()
    (other / "notes.txt").write_text("x")
    with pytest.raises(SystemExit, match="not a voice profile"):
        voz_setup.create_profile(other, "generic", None)
    with pytest.raises(SystemExit, match="outside the engine"):
        voz_setup.create_profile(ENGINE / "daemon" / "profile", "generic", None)


def test_dry_run_writes_nothing(home):
    profile = home / "voice-orchestrator"
    actions = voz_setup.create_profile(profile, "generic", "Alex", dry_run=True)
    assert actions and not profile.exists() and not (home / ".claude").exists()


def old_checkout(tmp_path: Path) -> Path:
    old = tmp_path / "old"
    (old / "context").mkdir(parents=True)
    (old / "daemon").mkdir()
    (old / "CLAUDE.md").write_text("Read `context/speaking.md` and `context/people.md`. SETUP.md has status. "
                                   "Run `python3 scripts/ledger.py snapshot`.\n")
    (old / "context" / "speaking.md").write_text("my rules\n")
    (old / "context" / "people.md").write_text("my people\n")
    engine_config = (ENGINE / "daemon" / "config.toml").read_text()
    (old / "daemon" / "config.toml").write_text(engine_config.replace("turn_gap_ms = 2500", "turn_gap_ms = 3000"))
    return old


def test_migration_copies_personal_files_byte_for_byte(home, tmp_path):
    old, profile = old_checkout(tmp_path), home / "voice-orchestrator"
    voz_setup.migrate(old, profile, "Sam", "generic", dry_run=False)
    assert (profile / "CLAUDE.md").read_bytes() == (old / "CLAUDE.md").read_bytes()
    assert (profile / "context" / "speaking.md").read_text() == "my rules\n"
    assert (profile / "context" / "people.md").read_text() == "my people\n"
    config = json.loads((home / ".claude/voice-orchestrator/config.json").read_text())
    assert config["user_name"] == "Sam"
    assert config["routine_titles"] == voz_setup.LEGACY_ROUTINE_TITLES
    tuning = (home / ".voz" / "config.toml").read_text()
    assert "turn_gap_ms = 3000" in tuning and "silence_ms" not in tuning
    assert voz_setup.dangling_references(profile) == ["SETUP.md"]


def test_migration_keeps_existing_state(home, tmp_path):
    old, profile = old_checkout(tmp_path), home / "voice-orchestrator"
    (home / ".voz").mkdir()
    (home / ".voz" / "config.toml").write_text("[vad]\nturn_gap_ms = 900\n")
    state = home / ".claude" / "voice-orchestrator"
    state.mkdir(parents=True)
    (state / "config.json").write_text('{"user_name": "Kept", "orchestrators": {"x": "y"}}')
    voz_setup.migrate(old, profile, "Other", "generic", dry_run=False)
    assert (home / ".voz" / "config.toml").read_text() == "[vad]\nturn_gap_ms = 900\n"
    config = json.loads((state / "config.json").read_text())
    assert config["user_name"] == "Kept" and config["orchestrators"] == {"x": "y"}


def test_checks_report_the_contract_shape(home):
    phases = voz_setup.run_checks(home / "voice-orchestrator", 1)
    assert {p.phase for p in phases} >= {"check", "dependencies", "models", "profile", "desktop"}
    assert all(p.state in {"pending", "running", "needs-user", "failed", "complete"} for p in phases)
    assert next(p for p in phases if p.phase == "profile").state == "pending"


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="macOS system Python not present")
def test_setup_runs_on_the_macos_system_python(home):
    run = subprocess.run(["/usr/bin/python3", str(ENGINE / "scripts/voz_setup.py"), "profile", "--dry-run"],
                         capture_output=True, text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert run.returncode == 0, run.stderr
    assert "would create" in run.stdout


def test_models_count_as_ready_only_at_the_pinned_revision(home, monkeypatch):
    cache = home / "hf"
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    required = voz_setup.required_models()
    assert required and len(voz_setup.missing_models()) == len(required)
    for repo, pin in required.items():
        folder = cache / ("models--" + repo.replace("/", "--"))
        for name in pin["files"]:
            f = folder / "snapshots" / pin["revision"] / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
        (folder / "refs").mkdir(parents=True, exist_ok=True)
        (folder / "refs" / "main").write_text(pin["revision"])
    assert voz_setup.missing_models() == []
    repo, pin = next(iter(required.items()))
    (cache / ("models--" + repo.replace("/", "--")) / "refs" / "main").write_text("0" * 40)
    assert voz_setup.missing_models() == [repo]

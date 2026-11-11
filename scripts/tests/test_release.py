"""The release check must pass on clean builds and fail on personal material."""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release  # noqa: E402

ENGINE = Path(__file__).resolve().parents[2]
needs_denylist = pytest.mark.skipif(not release.DENYLIST.is_file(), reason="the denylist stays in the development repo")


@pytest.fixture(scope="module")
def builds(tmp_path_factory):
    try:
        release.tracked_files(ENGINE)
    except release.NotACheckout:
        pytest.skip("release builds need a git checkout of the engine")
    out = {}
    for audience in release.AUDIENCES:
        target = tmp_path_factory.mktemp(audience) / "tree"
        problems = release.build(audience, target, None)
        out[audience] = (target, problems)
    return out


@pytest.mark.parametrize("audience", release.AUDIENCES)
def test_clean_build_has_no_problems(builds, audience):
    tree, problems = builds[audience]
    assert problems == []
    assert (tree / "profile-template" / "context" / "speaking.md").is_file()
    assert "MIT License" in (tree / "LICENSE").read_text()
    assert not (tree / "UPSTREAM.md").exists()
    for excluded in ("SETUP.md", "CLAUDE.md", "context", "release-denylist.txt", "daemon/ref"):
        assert not (tree / excluded).exists()


def test_outside_build_has_no_workplace_flavor(builds):
    tree, _ = builds["outside"]
    assert not (tree / "profile-template" / "flavors").exists()
    assert not (tree / "docs").exists()
    assert "internal:" not in (tree / "release-allowlist.txt").read_text()


# Planted text is assembled from pieces, so this file itself passes the check it tests.
@pytest.mark.parametrize("plant, expected", [
    pytest.param("Ask Mro" + "czek first.\n", "denylisted term", marks=needs_denylist),
    ("session local_1234abcd" + "-1111-2222-3333-444455556666\n", "session ID"),
    ("path /Users" + "/someone/Projects\n", "home-folder path"),
])
def test_check_fails_on_planted_text(builds, tmp_path, plant, expected):
    tree = tmp_path / "tree"
    shutil.copytree(builds["internal"][0], tree)
    with open(tree / "README.md", "a") as handle:
        handle.write(plant)
    problems = release.check(tree, "internal")
    assert any(expected in p for p in problems), problems


def test_check_fails_on_audio_state_and_unlisted_files(builds, tmp_path):
    tree = tmp_path / "tree"
    shutil.copytree(builds["internal"][0], tree)
    (tree / "daemon" / "ref").mkdir()
    (tree / "daemon" / "ref" / "me.wav").write_bytes(b"RIFF")
    (tree / "ledger.json").write_text("{}")
    (tree / "notes.md").write_text("hello\n")
    problems = "\n".join(release.check(tree, "internal"))
    assert "audio recordings never ship" in problems
    assert "local state" in problems
    assert "notes.md: not in the release allowlist" in problems


@needs_denylist
def test_outside_check_rejects_workplace_terms(builds, tmp_path):
    tree = tmp_path / "tree"
    shutil.copytree(builds["outside"][0], tree)
    with open(tree / "INSTALL.md", "a") as handle:
        handle.write("Post it in Sl" + "ack.\n")
    assert any("denylisted term" in p for p in release.check(tree, "outside"))
    assert not any("denylisted term" in p for p in release.check(tree, "internal"))


def test_placeholder_ids_in_tests_are_allowed():
    assert release.content_problems("x", "local_00000000-0000-0000-0000-000000000001", release.GENERIC_PATTERNS) == []


@needs_denylist
def test_author_credit_is_allowed_but_a_bare_first_name_is_not(builds, tmp_path):
    tree = tmp_path / "tree"
    shutil.copytree(builds["outside"][0], tree)
    assert "Spen" + "cer Mro" + "czek" in (tree / "README.md").read_text()
    assert release.check(tree, "outside") == []
    with open(tree / "README.md", "a") as handle:
        handle.write("Ask Spen" + "cer first.\n")
    assert any("denylisted term" in p for p in release.check(tree, "outside"))

"""Build a clean shared copy of the voice assistant, and check it for personal material.

  python3 scripts/release.py build --audience internal --out ../voz-release
  python3 scripts/release.py check --audience outside --dir ../voz-release

build copies only git-tracked files that release-allowlist.txt allows for the
audience, writes no git history, and then runs check on the result. check
fails when the tree has a file outside the allowlist, an audio recording,
local state, a home-folder path, a session ID, or a term from the denylist
(release-denylist.txt in the development repo, or --deny-file).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

ENGINE = Path(__file__).resolve().parent.parent
ALLOWLIST = "release-allowlist.txt"
DENYLIST = ENGINE / "release-denylist.txt"
AUDIENCES = ("internal", "outside")
AUDIO = {".wav", ".mp3", ".m4a", ".aiff", ".aif", ".flac", ".ogg", ".caf"}
STATE_NAMES = {"ledger.json", "decisions.jsonl", "daemon.json", "ref.txt", ".voz-profile.json"}
STATE_PARTS = {".venv", ".native", ".voz", "__pycache__", ".pytest_cache"}
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
GENERIC_PATTERNS = [
    (re.compile(r"/Users/(?!Shared/)[A-Za-z][\w.-]*"), "a home-folder path"),
    (re.compile(r"/home/[a-z][\w.-]*/"), "a home-folder path"),
    (re.compile(r"\blocal_" + UUID), "a Desktop session ID"),
    (re.compile(r"(?<![\w-])" + UUID + r"(?![\w-])"), "an ID that looks like a session ID"),
]
PLACEHOLDER_ID = re.compile(r"^(?:local_)?0{8}-0{4}-0{4}-0{4}-\d{12}$")
REQUIRED = {"LICENSE": "MIT License", "profile-template/context/speaking.md": "one breath"}


def audience_lines(path: Path, audience: str) -> List[str]:
    """Lines for this audience: unprefixed lines, plus lines prefixed with '<audience>:'."""
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        prefix, sep, rest = line.partition(":")
        if sep and prefix == "allow":
            continue
        if sep and prefix in AUDIENCES:
            if prefix == audience:
                out.append(rest.strip())
        else:
            out.append(line)
    return out


def allowed(rel: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in patterns)


class NotACheckout(RuntimeError):
    pass


def tracked_files(root: Path) -> List[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=str(root), capture_output=True, text=True)
    if result.returncode:
        raise NotACheckout(f"{root} is not a git checkout; a release is built from git-tracked files. "
                           f"Run the build from a clone. git said: {result.stderr.strip()}")
    return sorted(name for name in result.stdout.split("\0") if name and (root / name).is_file())


def filtered_allowlist(source: Path, audience: str) -> str:
    """The allowlist as shipped: comments kept, other audiences' lines removed."""
    keep = []
    for raw in source.read_text(encoding="utf-8").splitlines():
        prefix, sep, _ = raw.strip().partition(":")
        if sep and prefix in AUDIENCES and prefix != audience:
            continue
        keep.append(raw)
    return "\n".join(keep) + "\n"


def build(audience: str, out: Path, deny_file: Optional[Path]) -> List[str]:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty. Choose a new folder, so no old file slips into the release.")
    if out.resolve() == ENGINE or ENGINE in out.resolve().parents:
        raise SystemExit("Build the release outside the engine folder.")
    patterns = audience_lines(ENGINE / ALLOWLIST, audience)
    for rel in tracked_files(ENGINE):
        if allowed(rel, patterns):
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(ENGINE / rel), str(target))
    (out / ALLOWLIST).write_text(filtered_allowlist(ENGINE / ALLOWLIST, audience), encoding="utf-8")
    return check(out, audience, deny_file)


def walk(root: Path) -> List[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts)


def path_problems(rel: str, patterns: List[str]) -> List[str]:
    parts = set(Path(rel).parts)
    problems = []
    if Path(rel).suffix.lower() in AUDIO:
        problems.append(f"{rel}: audio recordings never ship")
    if Path(rel).name in STATE_NAMES or parts & STATE_PARTS:
        problems.append(f"{rel}: local state or build output never ships")
    if not allowed(rel, patterns):
        problems.append(f"{rel}: not in the release allowlist")
    return problems


def content_problems(rel: str, text: str, deny: List[Tuple[re.Pattern, str]],
                     allow: Optional[List[re.Pattern]] = None) -> List[str]:
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        for pattern in allow or []:
            line = pattern.sub("", line)
        for pattern, what in deny:
            match = pattern.search(line)
            if match and not PLACEHOLDER_ID.match(match.group(0)):
                problems.append(f"{rel}:{number}: {what} ({match.group(0)!r})")
    return problems


def denylist_source(deny_file: Optional[Path]) -> Optional[Path]:
    return deny_file or (DENYLIST if DENYLIST.is_file() else None)


def allow_patterns(deny_file: Optional[Path]) -> List[re.Pattern]:
    """Deliberate text, such as the author credit, from "allow:" lines of the denylist."""
    source = denylist_source(deny_file)
    if not source:
        return []
    lines = (line.strip() for line in source.read_text(encoding="utf-8").splitlines())
    return [re.compile(line[len("allow:"):].strip()) for line in lines if line.startswith("allow:")]


def deny_patterns(audience: str, deny_file: Optional[Path]) -> List[Tuple[re.Pattern, str]]:
    deny = list(GENERIC_PATTERNS)
    source = denylist_source(deny_file)
    if source:
        deny += [(re.compile(term, re.IGNORECASE), "a denylisted term") for term in audience_lines(source, audience)]
    return deny


def check(root: Path, audience: str, deny_file: Optional[Path] = None) -> List[str]:
    allowlist = root / ALLOWLIST
    if not allowlist.is_file():
        return [f"{ALLOWLIST} is missing from {root}"]
    patterns = audience_lines(allowlist, audience)
    deny, allow = deny_patterns(audience, deny_file), allow_patterns(deny_file)
    problems = []
    for rel in walk(root):
        problems += path_problems(rel, patterns)
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"{rel}: binary file; only text ships")
            continue
        problems += content_problems(rel, text, deny, allow)
    for rel, needle in REQUIRED.items():
        path = root / rel
        if not path.is_file() or needle not in path.read_text(encoding="utf-8"):
            problems.append(f"{rel}: required file or text is missing ({needle or 'file'})")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build and check a shared release")
    parser.add_argument("command", choices=["build", "check"])
    parser.add_argument("--audience", choices=AUDIENCES, required=True)
    parser.add_argument("--out", type=Path, help="new, empty folder for build")
    parser.add_argument("--dir", type=Path, default=ENGINE, help="tree to check")
    parser.add_argument("--deny-file", type=Path, help="extra or replacement denylist")
    args = parser.parse_args(argv)
    if args.command == "build" and not args.out:
        raise SystemExit("build needs --out <new folder>.")
    try:
        problems = build(args.audience, args.out, args.deny_file) if args.command == "build" else check(args.dir, args.audience, args.deny_file)
    except NotACheckout as e:
        raise SystemExit(str(e)) from e
    for problem in problems:
        print(problem)
    root = args.out if args.command == "build" else args.dir
    print(json.dumps({"audience": args.audience, "tree": str(root), "files": len(walk(root)), "problems": len(problems)}))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

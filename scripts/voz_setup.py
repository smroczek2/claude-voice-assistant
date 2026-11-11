"""First-run setup, doctor and profile tools for the voice assistant.

Runs on the macOS system Python (3.9) with the standard library only, so it
works before any dependency is installed.

  python3 scripts/voz_setup.py all       check, install, create a profile, print the Desktop steps
  python3 scripts/voz_setup.py check     the doctor: report every phase, change nothing
  python3 scripts/voz_setup.py install   Python packages, speech models, native audio helper
  python3 scripts/voz_setup.py profile   create or update a profile (never overwrites a file)
  python3 scripts/voz_setup.py migrate --from <old checkout>   move an existing voice folder to a profile

Each phase reports {phase, state, message, action}. State is one of
pending, running, needs-user, failed, complete. No phase claims that the
microphone, speech or session routing works; only a live test shows that.
"""

from __future__ import annotations

import argparse
import datetime as dt
import filecmp
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

ENGINE = Path(__file__).resolve().parent.parent
TEMPLATE = ENGINE / "profile-template"
PROFILE_MARKER = ".voz-profile.json"
SHIM_MARKER = "# Managed by voz setup."
TEMPLATE_VERSION = 1
MIN_FREE_GB = 15
MIN_MACOS = (14, 0)
DEFAULT_PORT = 8766
# Pinned model revisions and file hashes. Models marked required are what the default voice needs;
# the clone model is prepared only when ~/.voz/ref.wav exists.
MODEL_LOCK = ENGINE / "daemon" / "models.lock.json"
# The routine-job titles the ledger hid by default before version 0.3.0. Migration keeps them for old users.
LEGACY_ROUTINE_TITLES = ["vault sync", "vault compile-back", "plugin release", "commitment reconciler",
                         "account sync", "disk space cleanup", "standup"]
UV_INSTALL = "curl -LsSf https://astral.sh/uv/install.sh | sh"


@dataclass
class Phase:
    phase: str
    state: str
    message: str
    action: str = ""


def home() -> Path:
    return Path(os.path.expanduser("~"))


def default_profile() -> Path:
    return home() / "voice-orchestrator"


def state_dir() -> Path:
    return Path(os.path.expanduser(os.environ.get("VOZ_STATE_DIR") or "~/.claude/voice-orchestrator"))


def desktop_sessions_dir() -> Path:
    default = home() / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
    return Path(os.path.expanduser(os.environ.get("VOZ_DESKTOP_SESSIONS_DIR") or str(default)))


def hf_cache() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"]).expanduser()
    base = Path(os.environ.get("HF_HOME") or home() / ".cache" / "huggingface").expanduser()
    return base / "hub"


# ---------- checks (read-only) ----------

def check_platform() -> Phase:
    if platform.system() != "Darwin":
        return Phase("check", "failed", "This assistant runs only on macOS.")
    if not apple_silicon():
        return Phase("check", "failed", "This Mac is not Apple Silicon. The local speech models need an M1 or later.")
    version = tuple(int(part) for part in (platform.mac_ver()[0] or "0").split(".")[:2] + ["0"])[:2]
    if version < MIN_MACOS:
        return Phase("check", "failed", f"macOS {platform.mac_ver()[0]} is too old.", "Update to macOS 14 or later.")
    return Phase("check", "complete", f"Apple Silicon Mac, macOS {platform.mac_ver()[0]}.")


def apple_silicon() -> bool:
    """True on Apple Silicon, also when this Python runs under Rosetta."""
    if platform.machine() == "arm64":
        return True
    probe = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True)
    return probe.stdout.strip() == "1"


def check_tools() -> Phase:
    if not shutil.which("swift") or subprocess.run(["xcode-select", "-p"], capture_output=True).returncode:
        return Phase("check", "failed", "The Command Line Tools are missing. They give Swift, which builds the audio helper.",
                     "Run: xcode-select --install")
    return Phase("check", "complete", "Command Line Tools and Swift are installed.")


def find_uv() -> Optional[str]:
    for candidate in (shutil.which("uv"), "/opt/homebrew/bin/uv", str(home() / ".local/bin/uv"), str(home() / ".cargo/bin/uv")):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def check_uv() -> Phase:
    uv = find_uv()
    if not uv:
        return Phase("check", "failed", "uv is not installed. It installs the Python packages.", f"Run: {UV_INSTALL}")
    return Phase("check", "complete", f"uv found at {uv}.")


def required_models() -> Dict[str, dict]:
    models = json.loads(MODEL_LOCK.read_text())["models"]
    return {repo: pin for repo, pin in models.items() if pin.get("required")}


def missing_models() -> List[str]:
    """Required models whose pinned snapshot is incomplete or not the one the cache points at.
    Setup's install step downloads and hash-checks them (scripts/prepare_models.py)."""
    cache = hf_cache()
    missing = []
    for repo, pin in required_models().items():
        folder = cache / ("models--" + repo.replace("/", "--"))
        snapshot = folder / "snapshots" / pin["revision"]
        ref = folder / "refs" / "main"
        current = ref.read_text().strip() if ref.is_file() else ""
        if current != pin["revision"] or not all((snapshot / name).is_file() for name in pin["files"]):
            missing.append(repo)
    return missing


def check_disk() -> Phase:
    free_gb = shutil.disk_usage(str(home())).free / 1e9
    if not missing_models() or free_gb >= MIN_FREE_GB:
        return Phase("check", "complete", f"{free_gb:.0f} GB free.")
    return Phase("check", "failed", f"Only {free_gb:.0f} GB free. The models and packages need about {MIN_FREE_GB} GB.",
                 "Free some disk space, then run setup again.")


def check_port(port: int = DEFAULT_PORT) -> Phase:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return Phase("check", "needs-user", f"Port {port} is in use. If your voice is on, that is expected.",
                         "Otherwise set another port in the plugin's options.")
    return Phase("check", "complete", f"Port {port} is free.")


def check_desktop() -> Phase:
    folder = desktop_sessions_dir()
    if folder.is_dir():
        return Phase("desktop", "complete", "Claude Desktop session records found.")
    return Phase("desktop", "needs-user", "No Claude Desktop session records yet. The ledger will be empty until you use Desktop.",
                 "Install Claude Desktop and open one Code session, or set VOZ_DESKTOP_SESSIONS_DIR.")


def check_dependencies() -> Phase:
    if (ENGINE / "daemon" / ".venv" / "bin" / "voz-daemon").is_file():
        return Phase("dependencies", "complete", "Python packages are installed.")
    return Phase("dependencies", "pending", "Python packages are not installed yet.", "Run: ./setup.sh install")


def check_models() -> Phase:
    missing = missing_models()
    helper = (ENGINE / "daemon" / ".native" / "voice-audio").is_file()
    if not missing and helper:
        return Phase("models", "complete", "Speech models and the native audio helper are ready.")
    parts = ([f"{len(missing)} speech model(s) not downloaded at the pinned version"] if missing else []) + ([] if helper else ["audio helper not built"])
    return Phase("models", "pending", "; ".join(parts) + ".", "Run: ./setup.sh install")


def run_checks(profile: Optional[Path], port: int) -> List[Phase]:
    phases = [check_platform(), check_tools(), check_uv(), check_disk(), check_port(port),
              check_dependencies(), check_models()]
    if profile is not None:
        phases.append(check_profile(profile))
    phases.append(check_desktop())
    return phases


# ---------- profile ----------

def flavors() -> List[str]:
    """"generic" plus each folder in profile-template/flavors/ that this copy of the engine ships."""
    folder = TEMPLATE / "flavors"
    return ["generic"] + sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else ["generic"]


def template_files(flavor: str) -> Dict[str, Path]:
    """Relative path -> source file, base template plus the flavor overlay."""
    files = {str(p.relative_to(TEMPLATE)): p for p in TEMPLATE.rglob("*")
             if p.is_file() and "flavors" not in p.relative_to(TEMPLATE).parts}
    overlay = TEMPLATE / "flavors" / flavor
    if overlay.is_dir():
        files.update({str(p.relative_to(overlay)): p for p in overlay.rglob("*") if p.is_file()})
    return files


def profile_problem(profile: Path) -> Optional[str]:
    """Why setup must not write into this folder, or None."""
    resolved = profile.expanduser().resolve()
    if resolved == ENGINE or ENGINE in resolved.parents:
        return "The profile must live outside the engine folder, so engine updates never touch it."
    if resolved.is_file():
        return f"{profile} is a file, not a folder."
    if resolved.is_dir() and any(resolved.iterdir()) and not (resolved / PROFILE_MARKER).is_file():
        return f"{profile} already has files and is not a voice profile. Choose another folder with --profile."
    return None


def shim_text() -> str:
    return f'''{SHIM_MARKER} It runs the engine's ledger; setup rewrites this file.
import json, os, runpy, sys

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
engine = os.environ.get("VOZ_ENGINE")
if not engine:
    with open(os.path.join(here, "{PROFILE_MARKER}"), encoding="utf-8") as handle:
        engine = json.load(handle)["engine"]
script = os.path.join(engine, "scripts", "ledger.py")
if not os.path.isfile(script):
    sys.exit(f"The voice engine is not at {{engine}}. Run setup from the engine folder again.")
sys.path.insert(0, os.path.dirname(script))
sys.argv[0] = script
runpy.run_path(script, run_name="__main__")
'''


def write_managed(profile: Path, flavor: str, actions: List[str], dry_run: bool) -> None:
    """Engine-owned files: the ledger shim, the marker and the template copy. Rewritten every run."""
    shim = profile / "scripts" / "ledger.py"
    if shim.is_file() and SHIM_MARKER not in shim.read_text(encoding="utf-8"):
        actions.append(f"keep your own {shim} (not managed by setup)")
    else:
        put(shim, shim_text(), actions, dry_run)
    marker = profile / PROFILE_MARKER
    created = json.loads(marker.read_text(encoding="utf-8")).get("created") if marker.is_file() else dt.date.today().isoformat()
    record = {"engine": str(ENGINE), "flavor": flavor, "template_version": TEMPLATE_VERSION, "created": created}
    put(marker, json.dumps(record, indent=1) + "\n", actions, dry_run)
    for rel, source in template_files(flavor).items():
        put(profile / ".voz" / "template" / rel, source.read_text(encoding="utf-8"), actions, dry_run, quiet=True)


def put(path: Path, text: str, actions: List[str], dry_run: bool, quiet: bool = False) -> None:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    if not quiet:
        actions.append(f"write {path}")
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def copy_if_absent(target: Path, source: Path, actions: List[str], dry_run: bool) -> None:
    if target.exists():
        if not filecmp.cmp(str(target), str(source), shallow=False):
            actions.append(f"keep your {target}; the current template is in .voz/template/ to compare")
        return
    actions.append(f"create {target}")
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source), str(target))


def seed_state_config(values: Dict[str, object], actions: List[str], dry_run: bool) -> None:
    """Add keys to ~/.claude/voice-orchestrator/config.json only where they are absent."""
    path = state_dir() / "config.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    added = {k: v for k, v in values.items() if v is not None and k not in config}
    if not added:
        return
    actions.append(f"add {', '.join(sorted(added))} to {path}")
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**config, **added}, indent=1) + "\n", encoding="utf-8")


def create_profile(profile: Path, flavor: str, name: Optional[str], dry_run: bool = False) -> List[str]:
    problem = profile_problem(profile)
    if problem:
        raise SystemExit(problem)
    actions: List[str] = []
    for rel, source in template_files(flavor).items():
        copy_if_absent(profile / rel, source, actions, dry_run)
    write_managed(profile, flavor, actions, dry_run)
    seed_state_config({"user_name": name}, actions, dry_run)
    return actions


REFERENCE_RE = re.compile(r"`((?:[\w.-]+/)*[\w.-]+\.(?:md|py|json|toml))`|(?<![\w/~.-])((?:[\w-]+/)*[\w-]+\.md)\b")


# Template files that a profile may leave out; CLAUDE.md names them as optional.
OPTIONAL_REFERENCES = {"context/about-me.md", "context/workplace.md"}


def references(text: str) -> set:
    return {a or b for a, b in REFERENCE_RE.findall(text)}


def dangling_references(profile: Path) -> List[str]:
    """Relative file names in backticks in CLAUDE.md that do not exist in the profile."""
    claude = profile / "CLAUDE.md"
    if not claude.is_file():
        return []
    names = sorted(references(claude.read_text(encoding="utf-8")))
    return [n for n in names if n not in OPTIONAL_REFERENCES and not n.startswith(("~", "/")) and not (profile / n).exists()]


def check_profile(profile: Path) -> Phase:
    if not (profile / PROFILE_MARKER).is_file():
        return Phase("profile", "pending", f"No voice profile at {profile}.", "Run: ./setup.sh profile")
    missing = [rel for rel in ("CLAUDE.md", "context/speaking.md", "scripts/ledger.py") if not (profile / rel).is_file()]
    if missing:
        return Phase("profile", "failed", f"The profile lacks {', '.join(missing)}.", "Run: ./setup.sh profile")
    engine = json.loads((profile / PROFILE_MARKER).read_text(encoding="utf-8")).get("engine")
    if engine != str(ENGINE):
        return Phase("profile", "needs-user", f"The profile points to another engine folder ({engine}).",
                     "Run ./setup.sh profile from the engine you want to use.")
    dangling = dangling_references(profile)
    if dangling:
        return Phase("profile", "needs-user", f"CLAUDE.md names files the profile does not have: {', '.join(dangling)}.",
                     "Copy those files into the profile, or edit CLAUDE.md.")
    return Phase("profile", "complete", f"Profile ready at {profile}.")


# ---------- migration ----------

TOML_LINE = re.compile(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.+?)\s*(?:#.*)?$")


def toml_values(path: Path) -> Dict[str, str]:
    """section.key -> raw value text, for the flat key = value lines config.toml uses."""
    values, section = {}, ""
    for line in path.read_text(encoding="utf-8").splitlines():
        header = re.match(r"^\s*\[([^\]]+)\]", line)
        if header:
            section = header.group(1).strip()
            continue
        match = TOML_LINE.match(line)
        if match:
            values[f"{section}.{match.group(1)}"] = match.group(2)
    return values


def tuning_overrides(old: Path) -> str:
    """The keys where an old checkout's daemon/config.toml differs from this engine's, as a TOML file."""
    engine_values = toml_values(ENGINE / "daemon" / "config.toml")
    changed: Dict[str, Dict[str, str]] = {}
    for key, value in toml_values(old).items():
        if engine_values.get(key) != value:
            section, name = key.split(".", 1)
            changed.setdefault(section, {})[name] = value
    blocks = [f"[{section}]\n" + "".join(f"{k} = {v}\n" for k, v in keys.items()) for section, keys in changed.items()]
    return ("# Your voice tuning, moved from your old voice folder. Engine updates never change this file.\n\n"
            + "\n".join(blocks)) if blocks else ""


def personal_context(old: Path) -> List[str]:
    """context/*.md files the old CLAUDE.md names that this engine does not ship."""
    claude = old / "CLAUDE.md"
    names = references(claude.read_text(encoding="utf-8")) if claude.is_file() else set()
    return sorted({n for n in names if n.startswith("context/") and (old / n).is_file() and not (ENGINE / n).exists()})


def migrate(old: Path, profile: Path, name: Optional[str], flavor: str, dry_run: bool) -> List[str]:
    if not (old / "CLAUDE.md").is_file():
        raise SystemExit(f"{old} has no CLAUDE.md, so there is nothing to migrate.")
    problem = profile_problem(profile)
    if problem:
        raise SystemExit(problem)
    actions: List[str] = []
    for rel in ["CLAUDE.md", *personal_context(old)]:
        copy_if_absent(profile / rel, old / rel, actions, dry_run)
    write_managed(profile, flavor, actions, dry_run)
    seed_state_config({"user_name": name, "routine_titles": LEGACY_ROUTINE_TITLES}, actions, dry_run)
    migrate_tuning(old, actions, dry_run)
    return actions


def migrate_tuning(old: Path, actions: List[str], dry_run: bool) -> None:
    old_config, user_config = old / "daemon" / "config.toml", home() / ".voz" / "config.toml"
    if not old_config.is_file():
        return
    text = tuning_overrides(old_config)
    if not text:
        return
    if user_config.exists():
        actions.append(f"keep your {user_config}; compare it with {old_config} by hand")
        return
    put(user_config, text, actions, dry_run)


# ---------- install ----------

def stream(argv: List[str], cwd: Path) -> int:
    print(f"$ {' '.join(argv)}", flush=True)
    return subprocess.run(argv, cwd=str(cwd)).returncode


def install() -> List[Phase]:
    uv = find_uv()
    if not uv:
        return [check_uv()]
    daemon = ENGINE / "daemon"
    if stream([uv, "sync", "--frozen"], daemon):
        return [Phase("dependencies", "failed", "uv sync failed.", "Read the error above, fix it, then run ./setup.sh install again.")]
    if stream([uv, "run", "--frozen", "python", "../scripts/prepare_models.py"], daemon):
        return [Phase("dependencies", "complete", "Python packages are installed."),
                Phase("models", "failed", "Model preparation stopped. Finished downloads are kept.",
                      "Check the network and disk space, then run ./setup.sh install again. It resumes.")]
    return [check_dependencies(), check_models()]


# ---------- output ----------

def desktop_steps(profile: Path) -> Phase:
    message = ("In Claude Desktop, open the Code tab and add a saved Local environment: "
               f"folder {profile}, Worktree off, environment variable CLAUDE_CODE_PLUGIN_DIRS={ENGINE}. "
               "Start a session in it and run /voice-assistant. macOS then asks for microphone access for Claude.")
    return Phase("desktop", "needs-user", message, "Do these steps once; setup cannot change Desktop settings.")


def report(phases: List[Phase], as_json: bool) -> int:
    if as_json:
        print(json.dumps([asdict(p) for p in phases], indent=1))
    else:
        for p in phases:
            print(f"[{p.state}] {p.phase}: {p.message}" + (f"\n    -> {p.action}" if p.action else ""))
    return 1 if any(p.state == "failed" for p in phases) else 0


def print_actions(actions: List[str], dry_run: bool) -> None:
    prefix = "would " if dry_run else ""
    for action in actions or ["nothing to change"]:
        print(f"- {prefix}{action}")


def command_all(args) -> int:
    first = [check_platform(), check_tools(), check_uv(), check_disk()]
    if report(first, args.json):
        return 1
    if report(install(), args.json):
        return 1
    print_actions(create_profile(args.profile, args.flavor, args.name), False)
    return report([check_profile(args.profile), desktop_steps(args.profile),
                   Phase("test", "pending", "Speak to the new session to finish. Setup cannot test the microphone for you.")], args.json)


def parse_args(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="Voice assistant setup and doctor")
    parser.add_argument("command", choices=["all", "check", "install", "profile", "migrate"])
    parser.add_argument("--profile", type=lambda p: Path(p).expanduser(), default=None,
                        help="profile folder (default ~/voice-orchestrator)")
    parser.add_argument("--flavor", choices=flavors(), default="generic")
    parser.add_argument("--name", help="your first name, used in the ledger's output")
    parser.add_argument("--from", dest="source", type=lambda p: Path(p).expanduser(), help="old voice folder (migrate)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    args.profile = args.profile or default_profile()
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    commands: Dict[str, Callable[[], int]] = {
        "all": lambda: command_all(args),
        "check": lambda: report(run_checks(args.profile, args.port), args.json),
        "install": lambda: report(install(), args.json),
        "profile": lambda: print_actions(create_profile(args.profile, args.flavor, args.name, args.dry_run), args.dry_run) or 0,
        "migrate": lambda: command_migrate(args),
    }
    return commands[args.command]()


def command_migrate(args) -> int:
    if not args.source:
        raise SystemExit("migrate needs --from <your old voice folder>.")
    print_actions(migrate(args.source, args.profile, args.name, args.flavor, args.dry_run), args.dry_run)
    if not args.dry_run:
        return report([check_profile(args.profile), desktop_steps(args.profile)], args.json)
    for name in dangling_references_preview(args.source, args.profile):
        print(f"- note: CLAUDE.md names {name}, which the profile will not have. Copy it or edit CLAUDE.md.")
    return 0


def dangling_references_preview(old: Path, profile: Path) -> List[str]:
    claude = old / "CLAUDE.md"
    names = references(claude.read_text(encoding="utf-8"))
    planned = {"CLAUDE.md", "scripts/ledger.py", *personal_context(old)}
    return sorted(n for n in names if n not in planned | OPTIONAL_REFERENCES and not (profile / n).exists() and not n.startswith(("~", "/")))


if __name__ == "__main__":
    sys.exit(main())

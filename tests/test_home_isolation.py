"""Every test runs against a fresh fake home, never the developer's real one (K-51).

The `_home_isolation` autouse fixture in conftest.py points HOME (and
USERPROFILE) at a per-test directory, unsets CODEX_HOME / XDG_* so they
follow it, and re-roots every import-time Path constant of the dct modules
that was computed under the real home. These tests are the guard: they fail as soon as any dct module
value, `Path.home()` or a child process still resolves to the real home.

The real home is only ever READ here (a hash of the files a refresh would
rewrite); every write a test performs is audited to land under the fake home.
"""

import hashlib
import os
import subprocess
import sys
import types
from contextlib import contextmanager
from pathlib import Path, PurePath

import pytest
from conftest import CODE_ROOTS, REAL_HOME, import_all_dct_modules

from dct import paths
from dct.integrations import claude, manager
from dct.integrations.codex import CodexIntegration

pytestmark = pytest.mark.slow

_HOME_ENV = ("HOME", "USERPROFILE")
# Unset, so each falls back to the fake HOME (dct.paths, CodexIntegration).
_DERIVED_ENV = ("CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                "XDG_STATE_HOME", "CLAUDE_CONFIG_DIR")


def _under(path, root) -> bool:
    try:
        PurePath(path).relative_to(root)
    except ValueError:
        return False
    return True


def _points_at_real_home(value, fake_home: Path) -> bool:
    """True for a path (or absolute path string) inside the real home that is
    not the current fake home (which may itself live under the real home when
    the basetemp does). Type checks go through type(), never isinstance():
    a module global can be a proxy (Flask's `request`) whose __class__ raises."""
    kind = type(value)
    if issubclass(kind, str):
        if not os.path.isabs(value):
            return False
        value = PurePath(value)
    elif not issubclass(kind, PurePath):
        return False
    if any(_under(value, root) for root in CODE_ROOTS):
        return False  # a checkout or venv inside the home is code, not home state
    real_roots = {REAL_HOME, Path(os.path.realpath(REAL_HOME))}
    return (any(_under(value, root) for root in real_roots)
            and not _under(value, fake_home)
            and not _under(value, os.path.realpath(fake_home)))


def _values(obj, depth=0):
    """`obj` plus the members of containers it holds (bounded depth)."""
    yield obj
    if depth >= 3:
        return
    kind = type(obj)
    if issubclass(kind, dict):
        for key, value in obj.items():
            yield from _values(key, depth + 1)
            yield from _values(value, depth + 1)
    elif issubclass(kind, (list, tuple, set, frozenset)):
        for value in obj:
            yield from _values(value, depth + 1)


def _defaults(label: str, member, module_name: str):
    """(label, defaults) of a function the module defines itself."""
    if issubclass(type(member), (staticmethod, classmethod)):
        member = member.__func__
    if type(member) is types.FunctionType and member.__module__ == module_name:
        yield f"{label}()", member.__defaults__ or ()
        yield f"{label}()", member.__kwdefaults__ or {}


def _home_leaks(fake_home: Path) -> list[str]:
    """Every dct module value that still resolves under the real home:
    module globals (and containers of them), plus the class attributes and
    function defaults of what each module defines itself."""
    leaks = []
    for name, module in sorted(sys.modules.items()):
        if module is None or not (name == "dct" or name.startswith("dct.")):
            continue
        for attr, value in vars(module).items():
            label = f"{name}.{attr}"
            owners = [(label, value), *_defaults(label, value, name)]
            if issubclass(type(value), type) and value.__module__ == name:
                for member_name, member in vars(value).items():
                    member_label = f"{label}.{member_name}"
                    owners += [(member_label, member), *_defaults(member_label, member, name)]
            leaks += [where for where, candidate in owners
                      if any(_points_at_real_home(v, fake_home) for v in _values(candidate))]
    return leaks


def test_no_dct_module_value_points_under_the_real_home():
    import_all_dct_modules()
    assert Path.home() != REAL_HOME, "Path.home() is the real home"
    leaks = _home_leaks(Path.home())
    assert not leaks, (
        "dct values computed under the real home survived the isolation — the "
        f"autouse fixture must re-root them: {leaks}")


def test_known_import_time_constants_are_rerooted_onto_the_fake_home():
    """The constants the triage named (claude.py CLAUDE_*): each one keeps its
    path relative to the home, now under the fake one."""
    home = Path.home()
    assert home != REAL_HOME
    expected = {
        "CLAUDE_SKILLS_DIR": home / ".claude" / "skills",
        "CLAUDE_HOOKS_DIR": home / ".claude" / "hooks",
        "CLAUDE_JSON": home / ".claude.json",
        "CLAUDE_SETTINGS": home / ".claude" / "settings.json",
        "CLAUDE_REFERENCE": home / ".claude" / "dct-reference.md",
        "CLAUDE_MD": home / ".claude" / "CLAUDE.md",
    }
    assert {name: getattr(claude, name) for name in expected} == expected
    from dct import setup
    assert setup.CLAUDE_SKILLS_DIR == expected["CLAUDE_SKILLS_DIR"]


def test_home_lookups_and_env_resolve_to_a_fresh_fake_home():
    home = Path.home()
    assert not _points_at_real_home(home, home / "nowhere")
    assert home != REAL_HOME
    assert Path(os.path.expanduser("~")) == home
    assert home.is_dir() and not any(home.iterdir()), "the fake home starts empty"
    for var in _HOME_ENV:
        assert os.environ[var] == str(home), f"{var}={os.environ[var]!r}"
    assert not [var for var in _DERIVED_ENV if var in os.environ]
    codex = CodexIntegration()
    assert codex.home == home / ".codex" and codex.skills_path == home / ".agents" / "skills"
    assert paths.config_dir() == home / ".config" / paths.FAMILY
    assert paths.data_dir() == home / ".local" / "share" / paths.FAMILY
    assert paths.legacy_dir() == home / ".claude"


def test_child_processes_inherit_the_fake_home():
    script = (
        "import os\n"
        "from pathlib import Path\n"
        "from dct.integrations import claude\n"
        "from dct.integrations.codex import CodexIntegration\n"
        "print(Path.home())\n"
        "print(claude.CLAUDE_MD)\n"
        "print(CodexIntegration().instructions_path)\n"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         check=True).stdout.splitlines()
    home = Path.home()
    assert Path(out[0]) == home
    assert Path(out[1]) == home / ".claude" / "CLAUDE.md"
    assert Path(out[2]) == home / ".codex" / "AGENTS.md"


@pytest.mark.skipif(sys.platform != "darwin", reason="the xcrun git shim exists only on macOS")
def test_git_spawned_under_the_fake_home_is_not_the_xcrun_shim(tmp_path):
    """Under a fake HOME the /usr/bin/git shim re-resolves the developer dir
    on every spawn (20-30 s per release test); PATH must reach the real binary
    first, and that binary must still find its own helpers (git-core)."""
    path_git = subprocess.run(["sh", "-c", "command -v git"], capture_output=True, text=True,
                              check=True).stdout.strip()
    if os.path.realpath(path_git) == "/usr/bin/git":
        pytest.fail("git on PATH is still the xcrun shim under the fake home")
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    assert subprocess.run(["git", "-C", str(tmp_path / "repo"), "rev-parse", "--git-dir"],
                          capture_output=True, text=True, check=True).stdout.strip() == ".git"


_SEEN_HOMES: dict[str, Path] = {}


@pytest.mark.parametrize("turn", ["first", "second"])
def test_each_test_gets_its_own_home(turn):
    home = Path.home()
    assert home != REAL_HOME, "isolation missing — refusing to write into the real home"
    marker = home / ".claude" / "marker"
    assert not marker.exists(), "a previous test's home leaked into this one"
    marker.parent.mkdir(parents=True)
    marker.write_text(turn)
    _SEEN_HOMES[turn] = home
    if turn == "second" and "first" in _SEEN_HOMES:
        assert _SEEN_HOMES["first"] != home


# --- Regression: a real upgrade refresh against the controlled home ---------

_OLD_FULL_BLOCK_BODY = """\
## Cross-project tracking (dct)

`dct` is a cross-project work tracker — items, checkpoints, changelog,
sprint flags, and session handoffs — all in one user-scoped database.
Registered at `~/.claude.json` (MCP), skills at `~/.claude/skills/`,
hooks at `~/.claude/hooks/`.

### What's available

**Skills** (invoke via `/name`) — all model-facing descriptions carry a `[dct]`
prefix and trigger in any language:
- `/track` — create item (issue / todo / feature / idea / improvement)
- `/track-list` — list/search, priority grouped, hotspot detection
- `/clog` — add changelog entry
- `/pickup` — start-of-session: pick up the handoff, show sprint, propose first action
"""

# Audited filesystem mutations -> which positional args they WRITE (a symlink's
# or a copy's source is only read).
_WRITE_EVENTS = {"os.rename": (0, 1), "os.replace": (0, 1), "os.remove": (0,),
                 "os.rmdir": (0,), "os.mkdir": (0,), "os.symlink": (1,), "os.link": (1,),
                 "os.chmod": (0,), "os.truncate": (0,), "os.utime": (0,), "os.chown": (0,),
                 "shutil.copyfile": (1,), "shutil.copytree": (1,), "shutil.rmtree": (0,),
                 "shutil.move": (0, 1)}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_audit_log: list | None = None


def _audit(event, args):
    log = _audit_log
    if log is None:
        return
    if event == "open":
        path, mode, flags = args
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS)
        # Bytecode caching of a lazily imported module is the interpreter's
        # write, not the refresh's.
        if (writes and isinstance(path, (str, bytes, os.PathLike))
                and "__pycache__" not in os.fsdecode(path)):
            log.append((event, os.fsdecode(path)))
    elif event in _WRITE_EVENTS:
        log.extend((event, os.fsdecode(args[i])) for i in _WRITE_EVENTS[event]
                   if i < len(args) and isinstance(args[i], (str, bytes, os.PathLike)))


sys.addaudithook(_audit)


@contextmanager
def _record_writes():
    global _audit_log
    _audit_log = log = []
    try:
        yield log
    finally:
        _audit_log = None


def _real_home_fingerprint() -> dict:
    """READ-ONLY hash of the real-home files an unisolated refresh rewrites."""
    out = {}
    for rel in (".claude/CLAUDE.md", ".claude/dct-reference.md", ".codex/AGENTS.md"):
        path = REAL_HOME / rel
        try:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            out[rel] = None
    return out


def test_upgrade_refresh_rewrites_only_the_fake_home_claude_md():
    """K-51: `refresh_integrations` with REAL installers refreshes an old full
    dct block in <home>/.claude/CLAUDE.md — the fake one — and every write it
    makes lands under the fake home; the real home's files stay byte-identical."""
    home = Path.home()
    # Self-protection: never drive a real refresh unless isolation is in place.
    assert home != REAL_HOME and not _points_at_real_home(claude.CLAUDE_MD, home)
    assert _under(claude.CLAUDE_MD, home)

    user_text = "# my private rules\n\nkeep this line\n"
    claude_md = home / ".claude" / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    old = (f"{user_text}\n{claude.SNIPPET_START}\n{_OLD_FULL_BLOCK_BODY}"
           f"{claude.SNIPPET_END}\n")
    claude_md.write_text(old, encoding="utf-8")
    before_real = _real_home_fingerprint()

    with _record_writes() as writes:
        results = manager.refresh_integrations()

    refreshed = claude_md.read_text(encoding="utf-8")
    assert [r["client"] for r in results] == ["claude"], results
    assert refreshed.startswith(user_text)
    assert "dct-reference.md" in refreshed and "### What's available" not in refreshed
    assert (home / ".claude" / "dct-reference.md").is_file()
    assert list((home / ".claude").glob("CLAUDE.md.bak-dct-*")), "the refresh keeps a backup"

    assert writes, "the audit hook saw no write — the proof below would be vacuous"
    outside = sorted({path for _, path in writes
                      if not _under(os.path.realpath(path), os.path.realpath(home))
                      and not _under(path, home)})
    assert not outside, f"refresh wrote outside the fake home: {outside}"
    assert _real_home_fingerprint() == before_real

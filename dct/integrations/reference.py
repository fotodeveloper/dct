"""Shared reference ownership, installation and dynamic instruction rendering.

Reuse the established link-target record and unchanged-copy proof for both
clients. Diagnostics are read-only; the canonical asset survives uninstall.
"""
import os
from pathlib import Path

from dct import assets, paths
from dct.integrations.base import (
    _line_ending,
    _link_or_copy,
    _marked_copy_body,
    forget_managed_link,
    is_recorded_link,
    read_exact_text,
    record_managed_link,
)


def repair_remedy(client: str) -> str:
    if client == "claude":
        return (
            "`dct integrate repair claude` installs it as part of the full Claude Code "
            "integration repair: it registers the dct MCP server (an existing entry is "
            "kept), installs the skills and hook files, wires the hooks into "
            "~/.claude/settings.json, adds the dct pointer block to ~/.claude/CLAUDE.md "
            "(refreshes it if present) and installs this reference. Run it only if you "
            "want all of that."
        )
    return "Run `dct integrate repair codex` to repair MCP, skills, reference, instructions and hook definitions; hooks still need host trust."


def owned_reference(path: Path) -> bool:
    """dct's own reference install (K-57: proven, never guessed): a symlink
    resolving into the RUNNING package's assets dir (also a dangling one —
    a file an upgrade dropped — and the hand-made link to
    assets/claude-md-snippet.md, K-76), a symlink whose exact target dct
    recorded when it created it (``is_recorded_link`` — survives a move of
    the package: new venv, new Python, editable <-> wheel), or an unchanged
    marked copy. A dangling link or a path that merely looks like another
    dct install proves nothing."""
    if path.is_symlink():
        target = os.path.realpath(path)
        source = os.path.realpath(assets.assets_dir())
        return target.startswith(source + os.sep) or is_recorded_link(path)
    return _marked_copy_body(path) is not None


def reference_current(path: Path) -> bool:
    """The installed reference is the current packaged one (doctor's byte
    comparison, as for hook files)."""
    src = assets.dct_reference_path()
    try:
        if path.is_symlink():
            return path.resolve() == src.resolve()
        return _marked_copy_body(path) == src.read_bytes()
    except OSError:
        return False


def reference_state(path: Path | None = None, *, client: str = "codex", owned=None) -> dict:
    """Classify the installed reference (K-66): ``current`` / ``missing`` /
    ``outdated`` (dct's, but not the running package's reference) /
    ``foreign`` (not proven dct's — dct never touches it).

    Returns ``{"state", "path", "target" (link target or None), "message",
    "remedy" (None when current)}``. A ``foreign`` remedy names that specific
    file (and its link target) and asks the user to move it to an unused name
    or replace it after keeping what they need, then repair: a repair alone
    would preserve it again. It never carries a ready-made shell command or
    a fixed backup name (K-78): a pasted command breaks on spaces or quotes
    and could overwrite an earlier backup.
    """
    path = paths.dct_reference_path() if path is None else Path(path)
    owned = owned_reference if owned is None else owned
    remedy = repair_remedy(client)
    target = os.readlink(path) if path.is_symlink() else None
    report = {"path": str(path), "target": target}
    if target is None and not path.exists():
        return {**report, "state": "missing", "message": f"dct reference missing at {path}",
                "remedy": remedy}
    if not owned(path):
        if target is None:
            what = "a file dct did not install, or a copy edited since"
        else:
            dangling = "" if os.path.exists(path) else "dangling "
            what = f"a {dangling}link to {target} that dct did not create"
        subject = str(path) if target is None else f"{path} (the link itself, to {target})"
        return {**report, "state": "foreign",
                "message": f"{path} is not dct's ({what}) — dct leaves it untouched",
                "remedy": f"To use dct's reference, first move {subject} to an unused name, "
                          f"or replace it once you have kept what you need from it; dct never "
                          f"moves, replaces or removes it for you. After that, "
                          + remedy}
    if reference_current(path):
        return {**report, "state": "current", "message": f"Packaged dct reference at {path}",
                "remedy": None}
    was = f"dct's link to {target}" if target is not None else "dct's copy from an older package"
    return {**report, "state": "outdated",
            "message": f"Outdated dct reference at {path} ({was})", "remedy": remedy}


def install_reference(path: Path | None = None, *, client: str = "codex", create=None, owned=None) -> dict:
    """Install the canonical guide, or an explicitly supplied legacy alias.

    ``client`` selects the complete repair remedy. ``create`` and ``owned``
    preserve existing compatibility seams; defaults use shared exclusive
    creation and established ownership proof. Foreign paths are preserved.
    Recorded links survive package moves; unchanged copies can refresh.

    Return status (installed/refreshed/unchanged/preserved), path, mode,
    target, message and remedy. Creation I/O errors propagate, never become
    an installed result. A repeat of current bytes does not rewrite them.
    """
    src = assets.dct_reference_path()
    dst = paths.dct_reference_path() if path is None else Path(path)
    create = _link_or_copy if create is None else create
    state = reference_state(dst, client=client, owned=owned)
    if state["state"] == "foreign":
        return {"status": "preserved", "path": str(dst), "mode": None, "target": state["target"],
                "message": state["message"], "remedy": state["remedy"]}
    if state["state"] == "current":
        mode = "symlink" if dst.is_symlink() else "copy"
        if mode == "symlink" and not is_recorded_link(dst):
            record_managed_link(dst)  # adopt a current link made before records existed
        return {"status": "unchanged", "path": str(dst), "mode": mode, "target": state["target"],
                "message": f"dct reference current at {dst}", "remedy": None}
    present = state["state"] == "outdated"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if present:
        dst.unlink()
        forget_managed_link(dst)
    mode = create(src, dst, mark_file=True)
    if mode == "preserved":
        now = reference_state(dst, client=client, owned=owned)
        return {"status": "preserved", "path": str(dst), "mode": None, "target": now["target"],
                "message": f"{dst} appeared while dct was installing the reference — left untouched; "
                           + now["message"],
                "remedy": now["remedy"]}
    if mode == "symlink":
        record_managed_link(dst)
    status = "refreshed" if present else "installed"
    return {"status": status, "path": str(dst), "mode": mode, "target": str(src) if mode == "symlink" else None,
            "message": f"dct reference {status} at {dst}", "remedy": None}


def reference_row(*, client="codex", path=None, check="reference") -> dict:
    state = reference_state(path, client=client)
    return {"check": check, "status": "ok" if state["state"] == "current" else "error", **state}


def instruction_body(client: str, *, template: Path | None = None) -> str:
    """Render the same current resolved reference path for install and doctor."""
    source = template or (assets.claude_snippet_path() if client == "claude" else assets.assets_dir() / "codex-agents.md")
    text = source.read_text(encoding="utf-8")
    if client == "claude":
        if "\n---\n" not in text:
            raise ValueError(f"{source}: no '---' separator — cannot split off the preamble")
        text = text.split("\n---\n", 1)[1]
    text = text.replace("{{reference_path}}", str(paths.dct_reference_path()))
    if "{{" in text or "}}" in text:
        raise ValueError(f"{source}: unknown instruction template slot")
    return text.strip("\n")


def instruction_row(path: Path, body: str, start: str, end: str) -> dict:
    """An owned old-path block is outdated; malformed markers are foreign."""
    raw = read_exact_text(path) if path.is_file() else ""
    state = "missing"
    if raw.count(start) == raw.count(end) == 1 and raw.index(start) < raw.index(end):
        eol = _line_ending(raw)
        expected = eol.join((start, *body.split("\n"), end))
        actual = raw[raw.index(start):raw.index(end) + len(end)]
        state = "current" if actual == expected else "outdated"
    elif start in raw or end in raw:
        state = "foreign"
    return {"check": "instructions", "status": "ok" if state == "current" else "error", "state": state,
            "message": f"DCT managed instructions {state}: {path}"}

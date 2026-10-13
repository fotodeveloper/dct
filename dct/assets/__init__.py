"""Packaged asset locations — the single indirection for skill/hook dirs.

The PHYSICAL directories stay at dct/skills and dct/hooks: the host's
~/.claude symlinks point into them, so moving them is a breaking change
reserved for 2.0.0 (ADR #52 froze every public surface for 1.2.0). When
that window opens, this module is the one place that changes.
"""

from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent


def skills_dir() -> Path:
    return _PKG_ROOT / "skills"


def hooks_dir() -> Path:
    return _PKG_ROOT / "hooks"


def assets_dir() -> Path:
    """The packaged assets directory; an installed file resolving into it is
    dct's own (K-36 ownership of ~/.claude/dct-reference.md)."""
    return Path(__file__).resolve().parent


def claude_snippet_path() -> Path:
    """The packaged CLAUDE.md pointer `dct init` offers to attach (#443,
    K-36: a short block naming the reference, never the reference itself)."""
    return assets_dir() / "claude-md-snippet.md"


def dct_reference_path() -> Path:
    """The packaged dct reference read on demand (K-36): installed as
    ~/.claude/dct-reference.md, never pasted into CLAUDE.md."""
    return assets_dir() / "dct-reference.md"


def version_sync_template_path() -> Path:
    """The packaged generic version-sync template (K-76): every block
    commented; `dct project version-sync-script` writes it into a project."""
    return assets_dir() / "dct-version-sync.sh"

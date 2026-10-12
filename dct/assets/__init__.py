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


def claude_snippet_path() -> Path:
    """The packaged CLAUDE.md snippet `dct init` offers to attach (#443)."""
    return Path(__file__).resolve().parent / "claude-md-snippet.md"


def version_sync_template_path() -> Path:
    """The packaged generic version-sync template (K-76): every block
    commented; `dct project version-sync-script` writes it into a project."""
    return Path(__file__).resolve().parent / "dct-version-sync.sh"

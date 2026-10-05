"""Version contract + autodetect logic (K-44).

ONE contract for every version dct stores or computes: `X.Y` or `X.Y.Z` with
ASCII decimal components, optionally prefixed by `v`/`V` and surrounded by
whitespace. `normalize_version` strips the prefix and whitespace (and leading
zeros of each component) — callers store the NORMALIZED form, so a later bump
and a `v$CUR` tag never see `vv1.6.0`. Anything else is refused with
ValueError("Unsupported version ...") — never silently skipped.

`storable_version` is the ONE gate of every version WRITE (K-54, K-58):
normalize, then check the width of EVERY version column the value can land
in (ensure_version_fits — limits read from the schema, never typed in).
Release, `project set-version`, `project add` and auto-detection all pass
through it; prerelease / build suffixes are refused, not interpreted.

`detect_version` is separate: it reports whatever a project file says (raw)
and never raises; create_project runs its result through storable_version
and REPORTS an unsupported one instead of storing a substitute.
"""

import json
import re
import tomllib
from pathlib import Path

_VERSION_RE = re.compile(r"[vV]?([0-9]+)\.([0-9]+)(?:\.([0-9]+))?", re.ASCII)
SUPPORTED_FORMATS = "X.Y or X.Y.Z (optional leading 'v')"


def parse_version(version: str) -> tuple[int, ...]:
    """`(X, Y)` or `(X, Y, Z)` for a supported version; ValueError otherwise.

    Surrounding whitespace and one leading `v`/`V` are accepted.
    """
    match = _VERSION_RE.fullmatch(version.strip()) if isinstance(version, str) else None
    if match is None:
        raise ValueError(f"Unsupported version {version!r} — expected {SUPPORTED_FORMATS}")
    return tuple(int(part) for part in match.groups() if part is not None)


def format_version(parts: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in parts)


def normalize_version(version: str) -> str:
    """The canonical stored form: `v1.6.0` → `1.6.0`, ` 0.18 ` → `0.18`.

    Raises ValueError for an unsupported format (see module docstring).
    """
    return format_version(parse_version(version))


def ensure_version_fits(version: str) -> None:
    """ValueError ("version too long ...") unless `version` fits every column
    a version is stored in: projects.current_version,
    changelog_entries.version and releases.version (widths read from the
    schema, so widening or narrowing a column moves the guard)."""
    from dct.core.limits import ensure_fits
    from dct.db import changelog_entries, projects, releases

    for table, column in (
        (projects, "current_version"),
        (changelog_entries, "version"),
        (releases, "version"),
    ):
        ensure_fits(table, {column: version}, {column: "version"})


def storable_version(version: str) -> str:
    """The form a write stores: normalize_version + ensure_version_fits.

    Raises ValueError ("Unsupported version ..." / "version too long ...").
    """
    normalized = normalize_version(version)
    ensure_version_fits(normalized)
    return normalized


def version_key(version: str) -> tuple[int, ...]:
    """Comparable form: trailing zero components dropped, so 1.5 == 1.5.0.

    Raises ValueError for an unsupported format.
    """
    parts = list(parse_version(version))
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def compare_versions(a: str, b: str) -> int:
    """-1 / 0 / 1 as `a` is lower / equal / higher than `b` (1.5 == 1.5.0).

    Raises ValueError when either side is unsupported.
    """
    ka, kb = version_key(a), version_key(b)
    return (ka > kb) - (ka < kb)


def same_version(a: str | None, b: str | None) -> bool:
    """Equal under the contract (1.5 == 1.5.0 == v1.5); never raises.

    When either side is not a supported version (a legacy stored value), falls
    back to exact string equality; None equals only None.
    """
    if a is None or b is None:
        return a is b
    try:
        return version_key(a) == version_key(b)
    except ValueError:
        return a == b


def bump(current: str, bump_type: str) -> str:
    """Bump version string by type (patch/minor/major); result is normalized.

    Supports both X.Y (two-part) and X.Y.Z (three-part) formats, with an
    optional leading `v` on `current` (dropped from the result).
    Two-part versions get a patch level on patch bump: 0.18 → 0.18.1.
    Raises ValueError on an invalid bump type or an unsupported `current`.
    """
    if bump_type not in ("patch", "minor", "major"):
        raise ValueError(f"Invalid bump type: '{bump_type}'. Valid: patch, minor, major")

    parts = list(parse_version(current))

    if bump_type == "major":
        return format_version((parts[0] + 1, 0, 0) if len(parts) >= 3 else (parts[0] + 1, 0))
    elif bump_type == "minor":
        return format_version((parts[0], parts[1] + 1, 0) if len(parts) >= 3 else (parts[0], parts[1] + 1))
    else:  # patch
        if len(parts) >= 3:
            return format_version((parts[0], parts[1], parts[2] + 1))
        else:
            return format_version((parts[0], parts[1], 1))


def detect_version(path: str | Path) -> tuple[str | None, str | None]:
    """Best-effort version detection from common project files.

    Tries in order and returns the first hit:
      1. <path>/VERSION                     — raw single line, stripped
      2. <path>/pyproject.toml              — [project].version (PEP 621)
      3. <path>/package.json                — .version (npm/yarn)
      4. <path>/Cargo.toml                  — [package].version (rust)
      5. <path>/src-tauri/tauri.conf.json   — .version (Tauri v2) or
                                              .package.version (Tauri v1)

    Returns (version, source_file) or (None, None) if no pattern matches.
    Never raises — malformed files are silently skipped so `project add`
    proceeds even when one of these files is corrupt.
    """
    base = Path(path)
    if not base.is_dir():
        return None, None

    # 1. VERSION (raw text)
    vf = base / "VERSION"
    if vf.is_file():
        try:
            v = vf.read_text(encoding="utf-8").strip()
            if v:
                return v, "VERSION"
        except (OSError, UnicodeDecodeError):
            pass

    # 2. pyproject.toml ([project].version)
    pp = base / "pyproject.toml"
    if pp.is_file():
        try:
            with open(pp, "rb") as f:
                data = tomllib.load(f)
            v = (data.get("project") or {}).get("version")
            if isinstance(v, str) and v:
                return v, "pyproject.toml"
        except (OSError, tomllib.TOMLDecodeError):
            pass

    # 3. package.json (.version)
    pj = base / "package.json"
    if pj.is_file():
        try:
            with open(pj, encoding="utf-8") as f:
                data = json.load(f)
            v = data.get("version")
            if isinstance(v, str) and v:
                return v, "package.json"
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass

    # 4. Cargo.toml ([package].version)
    ct = base / "Cargo.toml"
    if ct.is_file():
        try:
            with open(ct, "rb") as f:
                data = tomllib.load(f)
            v = (data.get("package") or {}).get("version")
            if isinstance(v, str) and v:
                return v, "Cargo.toml"
        except (OSError, tomllib.TOMLDecodeError):
            pass

    # 5. Tauri — v2 puts version at top level, v1 under .package.version
    tc = base / "src-tauri" / "tauri.conf.json"
    if tc.is_file():
        try:
            with open(tc, encoding="utf-8") as f:
                data = json.load(f)
            v = data.get("version") or (data.get("package") or {}).get("version")
            if isinstance(v, str) and v:
                return v, "src-tauri/tauri.conf.json"
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass

    return None, None

"""Version bump + autodetect logic — supports X.Y and X.Y.Z formats."""

import json
import tomllib
from pathlib import Path


def parse_version(version: str) -> tuple[int, ...]:
    parts = version.strip().split(".")
    return tuple(int(p) for p in parts)


def format_version(parts: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in parts)


def bump(current: str, bump_type: str) -> str:
    """Bump version string by type (patch/minor/major).

    Supports both X.Y (two-part) and X.Y.Z (three-part) formats.
    Two-part versions get a patch level on patch bump: 0.18 → 0.18.1
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

"""Passive update check for dct (#356).

Non-blocking, offline-safe PyPI version probe with a 24h on-disk cache.

Two public entry points:

- ``check_for_update()`` — cheap, always non-blocking. Reads the cache; when it
  is missing or older than the TTL, spawns a *daemon* thread that queries PyPI
  (2s timeout) and rewrites the cache. NEVER blocks the caller, NEVER raises,
  silent on any failure (offline, DNS, HTTP, malformed JSON).
- ``get_cached_notice()`` — pure cache read, NO network. Returns a one-line
  human notice when the cached ``latest`` is strictly newer than the installed
  version, else ``None``.

Cache file: ``dct-update-check.json`` in the platform cache dir (e.g.
``~/.cache/dctools/``) — ``{checked_at, latest, current}`` where
``checked_at`` is a Unix timestamp (float seconds).

Opt-out: ``[update] check = false`` in the dct config file (default true)
disables both the background probe and the notice.
"""

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from dct import __version__, paths
from dct.config import load_config

PYPI_URL = "https://pypi.org/pypi/dctracker/json"
TTL_SECONDS = 24 * 60 * 60
NETWORK_TIMEOUT = 2.0


def _cache_path() -> Path:
    return paths.cache_dir() / "dct-update-check.json"


def _parse_version(value: str) -> tuple[int, ...]:
    """Best-effort numeric version tuple (stdlib only, no packaging dep).

    Splits on ``.`` and reads the leading run of digits from each part; stops at
    the first non-numeric part (so ``1.2.3rc1`` → ``(1, 2)``). Unparseable input
    yields ``()``. Deliberately lossy — enough to answer "is A strictly newer
    than B?" for normal semver, and conservative on anything exotic.
    """
    parts: list[int] = []
    for chunk in str(value).split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits == "":
            break
        parts.append(int(digits))
    return tuple(parts)


_PRERELEASE_RE = re.compile(r"(?i)[._-]?(a|alpha|b|beta|c|rc|pre|preview|dev)")


def _is_prerelease(value: str) -> bool:
    """True when ``value`` carries a PEP 440-style pre-release / dev suffix.

    Recognizes ``a``/``alpha``, ``b``/``beta``, ``c``/``rc``, ``pre``/``preview``
    and ``dev`` markers appended to the numeric release (e.g. ``1.2.3rc1``,
    ``1.2.3.dev1``, ``1.2.3-alpha``). Stdlib-only, best-effort: strips the leading
    numeric release then inspects the remainder.
    """
    text = str(value)
    m = re.match(r"\s*v?\d+(?:\.\d+)*", text)
    rest = text[m.end():] if m else text
    return bool(_PRERELEASE_RE.match(rest))


def _is_newer(latest: str, current: str) -> bool:
    """True iff ``latest`` is strictly newer than ``current`` (PEP 440-lite).

    Compares zero-padded numeric release tuples first. When those are EQUAL, a
    bare release outranks a pre-release of the same release: ``1.2.3`` is newer
    than ``1.2.3rc1``, and ``1.2.3rc1`` is NOT newer than ``1.2.3``. Two
    pre-releases of an equal release compare equal (not newer). Stdlib only.
    """
    a = _parse_version(latest)
    b = _parse_version(current)
    if not a:
        return False
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    if a != b:
        return a > b
    # Equal numeric release: latest is newer only if it is a final release while
    # current is a pre-release (a final supersedes its own pre-releases).
    return (not _is_prerelease(latest)) and _is_prerelease(current)


def _read_cache(path: Path) -> dict | None:
    """Parse the JSON cache, or ``None``. A partial/corrupt file (``ValueError``)
    or an unreadable path (``OSError``) is treated as a miss — never raises."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, data: dict) -> None:
    """Write the JSON cache atomically: a sibling temp file + ``os.replace`` so a
    concurrent reader never observes a partial write (duplicate concurrent probes
    are acceptable — they just overwrite each other's complete file). Silent on an
    unwritable cache dir."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(data))
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        pass  # unwritable cache dir → give up silently


def _fetch_latest_version(url: str = PYPI_URL, timeout: float = NETWORK_TIMEOUT) -> str:
    """Query PyPI JSON and return ``info.version``. Raises on any failure."""
    with urlopen(url, timeout=timeout) as resp:  # noqa: S310 — fixed https URL
        payload = json.load(resp)
    return str(payload["info"]["version"])


def _refresh_cache(
    cache_path: Path | None = None,
    *,
    url: str = PYPI_URL,
    timeout: float = NETWORK_TIMEOUT,
    current: str | None = None,
) -> None:
    """Fetch the latest version and rewrite the cache. Silent on any failure.

    This is the daemon-thread body of ``check_for_update``; exposed for direct
    (synchronous) testing.
    """
    path = cache_path or _cache_path()
    try:
        latest = _fetch_latest_version(url=url, timeout=timeout)
    except Exception:
        return  # offline / DNS / HTTP / malformed JSON — stay silent
    _write_cache(
        path,
        {
            "checked_at": time.time(),
            "latest": latest,
            "current": current or __version__,
        },
    )


def _cache_is_fresh(data: dict | None, now: float) -> bool:
    """True when a cache dict exists and its ``checked_at`` is within the TTL."""
    if not data:
        return False
    try:
        checked_at = float(data.get("checked_at", 0))
    except (TypeError, ValueError):
        return False
    return (now - checked_at) < TTL_SECONDS


def check_for_update(
    config=None,
    *,
    cache_path: Path | None = None,
    now: float | None = None,
) -> threading.Thread | None:
    """Refresh the update cache in the background if it is stale. Never blocks.

    Honors the ``[update] check`` opt-out. When the cache is missing/stale,
    spawns a daemon thread that queries PyPI and rewrites the cache, and returns
    that thread (useful for tests: ``t = check_for_update(...); t and t.join()``).
    Returns ``None`` when opted out, when the cache is still fresh, or on any
    error. NEVER raises.
    """
    try:
        cfg = config or load_config()
        if not cfg.update.check:
            return None
        path = cache_path or _cache_path()
        current_now = time.time() if now is None else now
        if _cache_is_fresh(_read_cache(path), current_now):
            return None
        thread = threading.Thread(
            target=_refresh_cache,
            kwargs={"cache_path": path},
            daemon=True,
        )
        thread.start()
        return thread
    except Exception:
        return None  # a passive nicety must never break a command


def get_cached_notice(
    cache_path: Path | None = None,
    *,
    current: str | None = None,
    config=None,
) -> str | None:
    """Return a one-line upgrade notice from cache, or ``None``. No network.

    Honors the ``[update] check`` opt-out (``config`` or ``load_config()``):
    returns ``None`` when disabled, so the opt-out suppresses the notice
    everywhere it is surfaced (CLI nudge and MCP server instructions), matching
    the module docstring. A notice is returned only when the cached ``latest``
    is strictly newer than the installed version (``current``, defaulting to the
    running ``__version__``). Silent on a missing/unreadable cache. Never raises.
    """
    try:
        cfg = config or load_config()
        if not cfg.update.check:
            return None
        data = _read_cache(cache_path or _cache_path())
        if not data:
            return None
        latest = data.get("latest")
        if not latest:
            return None
        cur = current or __version__
        if _is_newer(str(latest), str(cur)):
            return f"dct {latest} is available (installed {cur}) — run: dct upgrade"
        return None
    except Exception:
        return None


def detect_install_method(dist_name: str = "dctracker") -> str:
    """Detect how ``dct`` was installed: 'editable' / 'uv' / 'pipx' / 'pip'.

    - ``editable`` — the distribution's ``direct_url.json`` sets
      ``dir_info.editable = true`` (``pip install -e`` / ``uv pip install -e``).
      Upgraded by pulling the repo, not by a package manager. A plain ``file:``
      URL WITHOUT that flag (a local wheel or ``pip install .``) is NOT editable —
      it upgrades like any pip install.
    - ``uv`` — the interpreter/prefix lives under a ``uv/tools`` directory.
    - ``pipx`` — the interpreter/prefix lives under a ``pipx`` directory.
    - ``pip`` — fallback; upgraded via ``python -m pip install -U``.

    Detection order is editable → uv → pipx → pip. Path checks use the resolved
    ``sys.prefix`` / ``sys.executable`` (monkeypatch those to exercise them).
    """
    import sys

    du = _read_direct_url(dist_name)
    if du:
        dir_info = du.get("dir_info") or {}
        if not isinstance(dir_info, dict):  # corrupt metadata: degrade, don't crash
            dir_info = {}
        if dir_info.get("editable") is True:
            return "editable"

    sep = os.sep
    prefix = os.path.realpath(sys.prefix)
    executable = os.path.realpath(sys.executable)
    haystack = f"{sep}{prefix}{sep}|{sep}{executable}{sep}".lower()
    if f"{sep}uv{sep}tools{sep}" in haystack:
        return "uv"
    if f"{sep}pipx{sep}" in haystack:
        return "pipx"
    return "pip"


def _read_direct_url(dist_name: str = "dctracker") -> dict | None:
    """Return the parsed ``direct_url.json`` for a distribution, or ``None``.

    ``None`` covers missing, unreadable, and malformed files — including valid
    JSON that is not an object (PEP 610 mandates an object, but a corrupt file
    must degrade to "no metadata", never crash ``dct upgrade``).
    """
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        dist = distribution(dist_name)
        text = dist.read_text("direct_url.json")
    except (PackageNotFoundError, OSError, ValueError):
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def upgrade_command(method: str) -> list[str] | None:
    """The shell command that upgrades dct for a given install method.

    Returns ``None`` for ``editable`` (no automated upgrade — pull the repo).
    """
    import sys

    return {
        "uv": ["uv", "tool", "upgrade", dist_pkg()],
        "pipx": ["pipx", "upgrade", dist_pkg()],
        "pip": [sys.executable, "-m", "pip", "install", "-U", dist_pkg()],
    }.get(method)


def dist_pkg() -> str:
    """PyPI distribution name for dct."""
    return "dctracker"

"""Client integration contract and conservative user-file ownership helpers."""

import hashlib
import json
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

COPY_MARKER = '.dct-managed-copy.json'


class ClientIntegration(ABC):
    """Database-independent adapter; results must be JSON serializable."""

    name: str

    @abstractmethod
    def detect(self) -> bool: ...

    @abstractmethod
    def install_mcp(self) -> dict: ...

    @abstractmethod
    def install_skills(self) -> dict: ...

    @abstractmethod
    def install_instructions(self) -> dict: ...

    @abstractmethod
    def doctor(self) -> list[dict]:
        """Rows contain check, status (ok/warning/error), and message."""
        ...

    @abstractmethod
    def uninstall(self) -> dict: ...


def atomic_write(path: Path, text: str) -> str | None:
    """Back up existing bytes, then atomically replace UTF-8 content.

    A byte-identical write is a no-op. Unique temporary and backup names avoid
    collisions; original permissions survive replacement. This is not a lock
    for concurrent read-modify-write operations.
    Existing symlinks keep pointing at their target; backups and replacement
    happen beside that target. Dangling or cyclic symlinks are refused.
    """
    path = Path(path)
    if path.is_symlink():
        try:
            path = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f'{path}: cannot resolve symlink; nothing was written') from exc
    encoded = text.encode('utf-8')
    original = path.read_bytes() if path.exists() else None
    if original == encoded:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if original is not None and original.strip():
        stamp = datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')
        fd, name = tempfile.mkstemp(prefix=f'{path.name}.bak-dct-{stamp}-', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(original)
        backup = name
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.dct-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
        if path.exists():
            shutil.copymode(path, name)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return backup


def read_exact_text(path: Path) -> str:
    """UTF-8 text with line endings exactly as stored (no newline translation),
    so a rewrite of part of a user's file leaves every other byte identical."""
    return path.read_bytes().decode('utf-8')


def _line_ending(text: str) -> str:
    """The file's dominant line ending: CRLF when it has more CRLF than bare
    LF line ends, otherwise LF (also for a file without any line end)."""
    crlf = text.count('\r\n')
    return '\r\n' if crlf > text.count('\n') - crlf else '\n'


def managed_instructions(path: Path, body: str, start: str, end: str) -> dict:
    """Refresh exactly one managed block; reject ambiguous/incomplete markers.

    Bytes outside the markers stay identical — CRLF, mixed line endings and a
    missing final newline included; only the inserted block (and, on append,
    the line break(s) separating it from existing content) follows the file's
    dominant line ending. ``unchanged`` means the same byte comparison found
    nothing to write: no write and no backup.
    """
    original = read_exact_text(path) if path.exists() else ''
    eol = _line_ending(original)
    lines = body.replace('\r\n', '\n').strip('\n').split('\n')
    block = eol.join([start, *lines, end])
    if start in original and end not in original:
        raise ValueError(f'{path}: start marker without end marker; nothing was written')
    if end in original and start not in original:
        raise ValueError(f'{path}: end marker without start marker; nothing was written')
    if start in original or end in original:
        if original.count(start) != 1 or original.count(end) != 1:
            raise ValueError(f'{path}: malformed managed markers; nothing was written')
        left, right = original.index(start), original.index(end)
        if right < left:
            raise ValueError(f'{path}: reversed managed markers; nothing was written')
        updated = original[:left] + block + original[right + len(end):]
        if updated == original:
            return {'status': 'unchanged', 'backup': None, 'path': str(path)}
        status = 'refreshed'
    else:
        if not original:
            separator = ''
        elif original.endswith('\n'):
            separator = eol
        else:
            separator = eol + eol
        updated = original + separator + block + eol
        status = 'added'
    return {'status': status, 'backup': atomic_write(path, updated), 'path': str(path)}


# Provenance of the single-file links dct creates (K-57): the exact target
# string of each link, recorded beside it when dct creates it. It proves the
# link is dct's even after the package moved (new venv, new Python,
# editable <-> wheel) — a dangling link or a familiar-looking path does not.
LINK_RECORD = '.dct-managed-links.json'


def _read_link_record(directory: Path) -> dict:
    try:
        data = json.loads((directory / LINK_RECORD).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    links = data.get('links') if isinstance(data, dict) and data.get('owner') == 'dct' else None
    return {k: v for k, v in links.items() if isinstance(v, str)} if isinstance(links, dict) else {}


def _write_link_record(directory: Path, links: dict) -> None:
    """dct's own metadata: replaced atomically, never backed up."""
    record = directory / LINK_RECORD
    if not links:
        record.unlink(missing_ok=True)
        return
    fd, name = tempfile.mkstemp(prefix=f'.{LINK_RECORD}.', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'owner': 'dct', 'links': links}, stream, indent=2, sort_keys=True)
        os.replace(name, record)
    finally:
        Path(name).unlink(missing_ok=True)


def record_managed_link(link: Path) -> None:
    """Record the target of a symlink dct has just created at ``link``."""
    links = _read_link_record(link.parent)
    links[link.name] = os.readlink(link)
    _write_link_record(link.parent, links)


def forget_managed_link(link: Path) -> None:
    """Drop ``link`` from the record (removed or replaced by a copy)."""
    links = _read_link_record(link.parent)
    if links.pop(link.name, None) is not None:
        _write_link_record(link.parent, links)


def is_recorded_link(link: Path) -> bool:
    """True only for a symlink whose target is exactly the one dct recorded
    when it created it; a link the user re-pointed, or any link created
    without a record, is not proven dct's."""
    if not link.is_symlink():
        return False
    try:
        return _read_link_record(link.parent).get(link.name) == os.readlink(link)
    except OSError:
        return False


def _copy_inventory(path: Path) -> dict:
    """Hash only regular files; refuse links so removal cannot cross ownership."""
    inventory = {}
    for entry in sorted(path.rglob('*')):
        if entry.is_symlink():
            raise ValueError('managed copy contains a symlink')
        if entry.is_file() and entry != path / COPY_MARKER:
            inventory[entry.relative_to(path).as_posix()] = hashlib.sha256(entry.read_bytes()).hexdigest()
        elif entry.is_dir():
            inventory[entry.relative_to(path).as_posix() + '/'] = None
    return inventory


def mark_managed_copy(path: Path) -> None:
    """Record installed contents; hashes prove unchanged content, not identity."""
    (path / COPY_MARKER).write_text(json.dumps({'owner': 'dct', 'files': _copy_inventory(path)}), encoding='utf-8')


def is_managed_copy(path: Path) -> bool:
    """True only for an unchanged directory explicitly installed by DCT."""
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        marker = json.loads((path / COPY_MARKER).read_text(encoding='utf-8'))
        return marker.get('owner') == 'dct' and marker.get('files') == _copy_inventory(path)
    except (OSError, ValueError, AttributeError):
        return False

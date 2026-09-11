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


def managed_instructions(path: Path, body: str, start: str, end: str) -> dict:
    """Refresh exactly one managed block; reject ambiguous/incomplete markers."""
    original = path.read_text(encoding='utf-8') if path.exists() else ''
    block = f'{start}\n{body.strip(chr(10))}\n{end}'
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
        status = 'unchanged' if updated == original else 'refreshed'
    else:
        updated = (original.rstrip('\n') + '\n\n' if original.strip() else '') + block + '\n'
        status = 'added'
    return {'status': status, 'backup': atomic_write(path, updated), 'path': str(path)}


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

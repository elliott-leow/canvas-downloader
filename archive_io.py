"""Small filesystem helpers shared by course downloads and incremental sync."""
from pathlib import Path
from collections.abc import Mapping
from contextlib import contextmanager
import hashlib
import json
import tempfile


def lock_file(stream, blocking=True):
    """Use the platform's ordinary advisory file lock without an extra package."""
    try:
        import fcntl
    except ImportError:
        import msvcrt
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if not blocking and error.errno in (11, 13):
                raise BlockingIOError('Archive is already locked') from error
            raise
    else:
        fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))


@contextmanager
def course_lock(session, root):
    """Serialize CLI, ZIP and background writes to the same course index."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    held = vars(session).setdefault('_canvas_locks', set()) if session is not None else set()
    if root in held:
        yield
        return
    with safe_path(root, '.canvas.lock').open('a') as stream:
        lock_file(stream, blocking=False)
        held.add(root)
        try:
            yield
        finally:
            held.remove(root)


def trim_log(path, limit=2 * 1024 * 1024):
    """Keep recent log lines in the same inode, including for inherited append FDs."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size <= limit:
        return
    with path.open('r+b') as stream:
        stream.seek(-limit // 2, 2)
        stream.readline()
        tail = stream.read()
        stream.seek(0)
        stream.write(tail)
        stream.truncate()


def trim_connector_logs(project):
    directories = [Path(project) / '.connector-logs', Path.home() / 'Library/Logs/Canvas Connector']
    for directory in directories:
        if directory.is_symlink():
            continue
        for path in directory.glob('*.log'):
            try:
                trim_log(path)
            except OSError:
                pass  # Log maintenance must never prevent an archive refresh.


def atomic_write(path: Path, chunks):
    """Replace a file only after its complete contents have been written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".canvas-", delete=False) as stream:
            temporary = Path(stream.name)
            for chunk in chunks:
                stream.write(chunk)
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def available_path(directory, filename, overwrite=False):
    """Keep existing downloads unless replacement was explicitly requested."""
    path = Path(directory) / filename
    if overwrite:
        return path
    base, extension = path.stem, path.suffix
    counter = 1
    while path.exists():
        path = Path(directory) / f"{base}_{counter}{extension}"
        counter += 1
    return path


def safe_path(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in [path, *path.parents] if p != root):
        raise ValueError("Archive paths must remain inside the archive without symlinks")
    return path


def write_download(path, response, expected_size=None):
    """Validate a stream before replacing a complete file; return size and digest."""
    digest, size = hashlib.sha256(), 0
    headers = response.headers
    content_type = headers.get("Content-Type", "").lower() if isinstance(headers, Mapping) else ""
    def chunks():
        nonlocal size
        prefix = b""
        pending = []
        checked = False
        for chunk in response.iter_content(65536):
            if not chunk:
                continue
            size += len(chunk)
            digest.update(chunk)
            if not checked:
                pending.append(chunk)
                prefix += chunk[:512 - len(prefix)]
                if len(prefix) < 512 and size < 512:
                    continue
                check(prefix)
                checked = True
                yield from pending
                pending = []
            else:
                yield chunk
        if not checked:
            check(prefix)
            yield from pending
        if isinstance(expected_size, int) and expected_size >= 0 and size != expected_size:
            raise ValueError("Download size differs from Canvas metadata; previous file preserved")
    def check(prefix):
        if Path(path).suffix.lower() not in {".html", ".htm"}:
            start = prefix.lstrip().lower()
            if content_type.startswith("text/html") or start.startswith((b"<!doctype html", b"<html")):
                raise ValueError("Canvas returned an HTML page instead of the requested file")
    atomic_write(path, chunks())
    return {"size": size, "sha256": digest.hexdigest()}


class DownloadIndex:
    """Stable item identities for one course, shared by module and course collectors."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = safe_path(self.root, '.canvas-index.json')
        self.items = json.loads(self.path.read_text()) if self.path.is_file() else {}
        self.seen = set()
        self.dirty = 0

    def target(self, key, directory, filename):
        previous = self.items.get(key, {})
        if previous.get('path'):
            old = safe_path(self.root, previous['path'])
            if (old.is_file() and previous.get('sha256') and old.stat().st_mtime_ns != previous.get('mtime_ns')
                    and hashlib.sha256(old.read_bytes()).hexdigest() != previous['sha256']):
                return available_path(old.parent, old.name)
            return old
        relative = Path(directory).resolve().relative_to(self.root) / filename
        return available_path(safe_path(self.root, relative.parent), relative.name)

    def valid(self, key, version=None, force=False):
        previous = self.items.get(key, {})
        if force or not previous.get('path'):
            return False
        path = safe_path(self.root, previous['path'])
        if not path.is_file() or path.stat().st_size != previous.get('size'):
            return False
        if previous.get('sha256') and previous.get('mtime_ns') != path.stat().st_mtime_ns:
            if hashlib.sha256(path.read_bytes()).hexdigest() != previous['sha256']:
                return False
        if version and any(version[:2]):
            if previous.get('version') is None and key in self.seen:
                return True
            return previous.get('version') == version
        return key in self.seen

    def record(self, key, path, version, integrity):
        self.items[key] = dict(path=Path(path).relative_to(self.root).as_posix(), version=version,
                               mtime_ns=Path(path).stat().st_mtime_ns, **integrity)
        self.seen.add(key)
        self.dirty += 1
        if self.dirty >= 25:
            self.flush()

    def flush(self):
        if self.dirty:
            atomic_write(self.path, [json.dumps(self.items, indent=2).encode()])
            self.dirty = 0

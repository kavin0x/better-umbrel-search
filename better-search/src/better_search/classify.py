from __future__ import annotations

import logging
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

JUNK_NAMES = {
    ".ds_store",
    "thumbs.db",
    "desktop.ini",
    ".localized",
}

JUNK_DIRS = {
    "system volume information",
    "$recycle.bin",
    ".trashes",
    ".spotlight-v100",
    ".fseventsd",
    ".temporaryitems",
    ".git",
    "node_modules",
    "__pycache__",
}

VIDEO_EXT = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".wmv",
    ".m4v",
    ".flv",
    ".mpeg",
    ".mpg",
    ".m2ts",
    ".ts",
}

ARCHIVE_EXT = {
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".iso",
    ".dmg",
    ".cab",
    ".zst",
}

BINARY_EXT = {
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".o",
    ".a",
    ".pyc",
    ".class",
    ".wasm",
    ".apk",
    ".msi",
    ".img",
    ".sys",
}

DOC_EXT = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".odt",
    ".rtf",
    ".txt",
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".csv",
    ".epub",
}

IMAGE_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}

DOC_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.text",
    "application/rtf",
    "text/rtf",
    "text/plain",
    "text/markdown",
    "text/html",
    "text/csv",
    "application/epub+zip",
    "application/xhtml+xml",
}

IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/tiff",
    "image/heic",
    "image/heif",
}

EXTERNAL_SOURCE_HINTS = ("/media/", "/run/media/", "/volumes/", "usb")


@dataclass(frozen=True)
class Decision:
    action: str
    file_type: str
    reason: str | None = None
    mime: str | None = None


def _skip(reason: str, mime: str | None = None) -> Decision:
    return Decision("skip", "skipped", reason, mime)


def _index(file_type: str, mime: str | None = None) -> Decision:
    return Decision("index", file_type, None, mime)


def default_mime(path: Path) -> str | None:
    try:
        import magic
    except ImportError:
        return None
    try:
        return magic.from_file(str(path), mime=True)
    except Exception:
        return None


def classify(
    path: Path,
    *,
    max_bytes: int,
    mime_of: Callable[[Path], str | None] | None = None,
) -> Decision:
    if path.is_symlink():
        return _skip("symlink")
    name = path.name.casefold()
    if name in JUNK_NAMES or name.startswith("._"):
        return _skip("junk-name")
    for part in path.parts[:-1]:
        if part.casefold() in JUNK_DIRS:
            return _skip("junk-directory")
    ext = path.suffix.casefold()
    if ext in VIDEO_EXT:
        return _skip("video")
    if ext in ARCHIVE_EXT:
        return _skip("archive")
    if ext in BINARY_EXT:
        return _skip("binary")
    try:
        size = path.stat().st_size
    except OSError:
        return _skip("unreadable")
    if size > max_bytes:
        return _skip("too-large")
    if size == 0:
        return _skip("empty")
    if ext in DOC_EXT:
        return _index("document")
    if ext in IMAGE_EXT:
        return _index("image")
    mime = (mime_of or default_mime)(path)
    if mime in DOC_MIMES or (mime or "").startswith("text/"):
        return _index("document", mime)
    if mime in IMAGE_MIMES:
        return _index("image", mime)
    if mime and (mime.startswith("video/") or mime.startswith("audio/")):
        return _skip("video", mime)
    if mime in {"application/zip", "application/x-7z-compressed", "application/gzip", "application/x-tar"}:
        return _skip("archive", mime)
    return _skip("unsupported", mime)


def looks_external(*, removable: str | None, mount_source: str | None) -> bool:
    if removable is not None and removable.strip() == "1":
        return True
    source = (mount_source or "").casefold()
    return any(hint in source for hint in EXTERNAL_SOURCE_HINTS)


def block_removable_flag(path: Path) -> str | None:
    try:
        dev = path.stat().st_dev
    except OSError:
        return None
    flag = Path(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}/removable")
    try:
        if flag.is_file():
            return flag.read_text(encoding="utf-8")
    except OSError:
        return None
    return None


def mount_source_for(path: Path) -> str | None:
    try:
        text = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    target = str(path.resolve())
    best: tuple[int, str] | None = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        source, mountpoint = parts[0], parts[1].replace("\\040", " ")
        if target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/"):
            if best is None or len(mountpoint) > best[0]:
                best = (len(mountpoint), source)
    return None if best is None else best[1]


def library_is_external(path: Path) -> bool:
    return looks_external(removable=block_removable_flag(path), mount_source=mount_source_for(path))


def device_predicate(root: Path) -> Callable[[Path], bool]:
    root_dev = root.stat().st_dev

    def same(path: Path) -> bool:
        try:
            st = path.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode):
            return False
        return st.st_dev == root_dev

    return same


def iter_library_files(root: Path, same_device: Callable[[Path], bool]) -> Iterator[Path]:
    """Yield files that stay on the library's own disk.

    Directories on another device are external mounts. They are not entered,
    so files on USB drives and other disks are never indexed.
    """
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        kept: list[str] = []
        for name in dirnames:
            child = current / name
            if name.casefold() in JUNK_DIRS:
                continue
            try:
                if child.is_symlink() or not same_device(child):
                    if not child.is_symlink():
                        logger.info("skip external mount %s", child)
                    continue
            except OSError:
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            file_path = current / name
            try:
                if file_path.is_symlink() or not same_device(file_path):
                    continue
            except OSError:
                continue
            yield file_path

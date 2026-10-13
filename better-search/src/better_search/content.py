from __future__ import annotations

from pathlib import Path


def safe_library_file(library: Path, raw: str) -> Path | None:
    if not raw or "\x00" in raw:
        return None
    try:
        library_root = library.resolve()
    except OSError:
        return None
    unresolved = library_root / raw
    probe = unresolved
    while probe != library_root and probe != probe.parent:
        if probe.is_symlink():
            return None
        probe = probe.parent
    candidate = unresolved.resolve(strict=False)
    try:
        candidate.relative_to(library_root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    try:
        if candidate.stat().st_dev != library_root.stat().st_dev:
            return None
    except OSError:
        return None
    return candidate

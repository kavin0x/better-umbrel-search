from __future__ import annotations

from pathlib import Path


def safe_library_file(library: Path, raw: str) -> Path | None:
    if not raw or "\x00" in raw:
        return None
    try:
        library_root = library.resolve()
    except OSError:
        return None
    candidate = (library_root / raw).resolve(strict=False)
    try:
        candidate.relative_to(library_root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate

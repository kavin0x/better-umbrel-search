from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextChunk:
    text: str
    page: int | None
    ordinal: int


def pack_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    collapsed = " ".join(text.split())
    if not collapsed:
        return []
    if len(collapsed) <= size:
        return [collapsed]
    chunks: list[str] = []
    start = 0
    while start < len(collapsed):
        end = min(len(collapsed), start + size)
        if end < len(collapsed):
            space = collapsed.rfind(" ", start + size // 2, end)
            if space != -1:
                end = space
        piece = collapsed[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(collapsed):
            break
        start = max(end - overlap, start + 1)
    return chunks


def chunks_from_pages(pages: list[tuple[str, int | None]]) -> list[TextChunk]:
    out: list[TextChunk] = []
    ordinal = 0
    for text, page in pages:
        for part in pack_text(text):
            out.append(TextChunk(part, page, ordinal))
            ordinal += 1
    return out

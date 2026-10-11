from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from better_search.db import Database
from better_search.embed import Embedder
from better_search.rrf import reciprocal_rank_fusion

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Hit:
    path: str
    name: str
    file_type: str
    page: int | None
    snippet: str
    score: float


@dataclass(frozen=True)
class SearchResponse:
    hits: list[Hit]
    took_ms: int


def fts_match(query: str) -> str | None:
    tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)
    if not tokens:
        return None
    return " AND ".join(f'"{token}"' for token in tokens)


def snippet_for(text: str, query: str, window: int = 240) -> str:
    compact = " ".join(text.split())
    if len(compact) <= window:
        return compact
    haystack = compact.casefold()
    found_at = -1
    for token in re.findall(r"[\w]+", query, flags=re.UNICODE):
        found_at = haystack.find(token.casefold())
        if found_at != -1:
            break
    if found_at == -1:
        return compact[:window].rstrip() + "…"
    start = max(0, found_at - 80)
    piece = compact[start : start + window].strip()
    if start > 0:
        piece = "…" + piece
    if start + window < len(compact):
        piece += "…"
    return piece


def in_folder(path: str, folder: str | None) -> bool:
    if not folder:
        return True
    cleaned = folder.strip("/")
    if not cleaned:
        return True
    return path == cleaned or path.startswith(cleaned + "/")


def search(
    db: Database,
    embedder: Embedder,
    query: str,
    *,
    file_type: str | None = None,
    folder: str | None = None,
    limit: int = 20,
) -> SearchResponse:
    started = time.perf_counter()
    limit = min(max(limit, 1), 50)
    match = fts_match(query)
    if match is None:
        return SearchResponse([], int((time.perf_counter() - started) * 1000))
    pool = min(max(limit * 8, 40), 100)
    keyword_ids = db.fts_ids(match, pool)
    text_ids: list[int] = []
    image_ids: list[int] = []
    try:
        text_ids = db.vec_ids("vec_text", embedder.embed_query(query), pool)
    except Exception:
        logger.exception("text vector search failed")
    try:
        image_ids = db.vec_ids("vec_image", embedder.embed_image_query(query), pool)
    except Exception:
        logger.exception("image vector search failed")
    fused = reciprocal_rank_fusion([keyword_ids, text_ids, image_ids])
    hits: list[Hit] = []
    seen: set[str] = set()
    for chunk_id, score in fused:
        info = db.chunk_info(chunk_id)
        if info is None:
            continue
        if file_type and info["file_type"] != file_type:
            continue
        if not in_folder(info["path"], folder):
            continue
        if info["path"] in seen:
            continue
        seen.add(info["path"])
        page = info["page"]
        hits.append(
            Hit(
                path=info["path"],
                name=info["name"],
                file_type=info["file_type"],
                page=None if page is None else int(page),
                snippet=snippet_for(info["text"], query),
                score=round(float(score), 6),
            )
        )
        if len(hits) >= limit:
            break
    took_ms = int((time.perf_counter() - started) * 1000)
    return SearchResponse(hits, took_ms)

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
    similarity: float | None


@dataclass(frozen=True)
class SearchResponse:
    hits: list[Hit]
    took_ms: int


# Quoted FTS tokens skip the Porter stemmer, so "grandmas" never hit a file
# named grandma.jpg. These words are dropped so a sentence still matches the
# names inside it: "research paper made by anup shah" keeps anup and shah.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "made",
        "me",
        "my",
        "not",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "who",
        "whom",
        "with",
    }
)

# Image-only neighbors below this cosine are CLIP guesses (a person for
# "grandmas"), not a photo the query actually describes.
IMAGE_ONLY_FLOOR = 0.32


def query_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[\w]+", query, flags=re.UNICODE):
        folded = token.casefold()
        if folded in _STOPWORDS or folded in seen:
            continue
        seen.add(folded)
        tokens.append(folded)
    if tokens:
        return tokens
    for token in re.findall(r"[\w]+", query, flags=re.UNICODE):
        folded = token.casefold()
        if folded not in seen:
            seen.add(folded)
            tokens.append(folded)
    return tokens


def fts_match(query: str) -> str | None:
    tokens = query_tokens(query)
    if not tokens:
        return None
    # Unquoted so the Porter tokenizer stems both sides. OR so one missing
    # filler word cannot hide a file that contains the real names.
    return " OR ".join(tokens)


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


def _dot(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True))))


def _stored_similarity(
    db: Database,
    chunk_id: int,
    text_query: list[float] | None,
    image_query: list[float] | None,
) -> float | None:
    scores: list[float] = []
    if text_query is not None:
        stored = db.embedding("vec_text", chunk_id)
        if stored is not None:
            scores.append(_dot(stored, text_query))
    if image_query is not None:
        stored = db.embedding("vec_image", chunk_id)
        if stored is not None:
            scores.append(_dot(stored, image_query))
    if not scores:
        return None
    return max(scores)


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
    keyword_set = set(keyword_ids)
    text_ids: list[int] = []
    image_ids: list[int] = []
    similarities: dict[int, float] = {}
    text_query: list[float] | None = None
    image_query: list[float] | None = None
    try:
        text_query = embedder.embed_query(query)
        for chunk_id, similarity in db.vec_ids("vec_text", text_query, pool):
            text_ids.append(chunk_id)
            similarities[chunk_id] = max(similarities.get(chunk_id, 0.0), similarity)
    except Exception:
        logger.exception("text vector search failed")
    try:
        image_query = embedder.embed_image_query(query)
        # A filename hit already answers the query. Keep only confident photo
        # matches so a loose CLIP neighbor cannot sit above that file.
        image_distance = 1.0 - IMAGE_ONLY_FLOOR if keyword_ids else 0.75
        for chunk_id, similarity in db.vec_ids("vec_image", image_query, pool, max_distance=image_distance):
            if chunk_id not in keyword_set and similarity < IMAGE_ONLY_FLOOR:
                continue
            image_ids.append(chunk_id)
            similarities[chunk_id] = max(similarities.get(chunk_id, 0.0), similarity)
    except Exception:
        logger.exception("image vector search failed")
    # Keyword order is bm25, with the file name weighted above body text.
    # Those hits stay ahead of vector-only neighbors.
    keyword_ranked = [(chunk_id, 1.0 + 1.0 / (60 + rank)) for rank, chunk_id in enumerate(keyword_ids, start=1)]
    fused_rest = reciprocal_rank_fusion(
        [
            [chunk_id for chunk_id in text_ids if chunk_id not in keyword_set],
            [chunk_id for chunk_id in image_ids if chunk_id not in keyword_set],
        ]
    )
    fused = keyword_ranked + fused_rest
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
        similarity = similarities.get(chunk_id)
        if similarity is None:
            similarity = _stored_similarity(db, chunk_id, text_query, image_query)
        hits.append(
            Hit(
                path=info["path"],
                name=info["name"],
                file_type=info["file_type"],
                page=None if page is None else int(page),
                snippet=snippet_for(info["text"], query),
                score=round(float(score), 6),
                similarity=None if similarity is None else round(similarity, 4),
            )
        )
        if len(hits) >= limit:
            break
    took_ms = int((time.perf_counter() - started) * 1000)
    return SearchResponse(hits, took_ms)

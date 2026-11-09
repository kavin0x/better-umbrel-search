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

# Vector-only neighbors below these cosines are loose guesses: a person for
# "grandmas", or a random note for a name that is not in the file.
IMAGE_ONLY_FLOOR = 0.42
TEXT_ONLY_FLOOR = 0.45


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


def _forms(token: str) -> tuple[str, ...]:
    forms = [token]
    if len(token) >= 5 and token.endswith("s"):
        forms.append(token[:-1])
    if len(token) >= 6 and token.endswith("es"):
        forms.append(token[:-2])
    return tuple(dict.fromkeys(forms))


def token_hits(text: str, tokens: list[str]) -> int:
    folded = text.casefold()
    return sum(1 for token in tokens if any(form in folded for form in _forms(token)))


def snippet_for(text: str, query: str, window: int = 240) -> str:
    compact = " ".join(text.split())
    if len(compact) <= window:
        return compact
    tokens = query_tokens(query)
    haystack = compact.casefold()
    positions: list[int] = []
    for token in tokens:
        for form in _forms(token):
            start = 0
            while True:
                found_at = haystack.find(form, start)
                if found_at == -1:
                    break
                positions.append(found_at)
                start = found_at + len(form)
    if not positions:
        return compact[:window].rstrip() + "…"
    best_start = 0
    best_count = -1
    for found_at in positions:
        start = max(0, found_at - 80)
        piece = haystack[start : start + window]
        count = sum(1 for token in tokens if any(form in piece for form in _forms(token)))
        if count > best_count:
            best_count = count
            best_start = start
    piece = compact[best_start : best_start + window].strip()
    if best_start > 0:
        piece = "…" + piece
    if best_start + window < len(compact):
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
        for chunk_id, similarity in db.vec_ids(
            "vec_text", text_query, pool, max_distance=1.0 - TEXT_ONLY_FLOOR
        ):
            if chunk_id not in keyword_set and similarity < TEXT_ONLY_FLOOR:
                continue
            text_ids.append(chunk_id)
            similarities[chunk_id] = max(similarities.get(chunk_id, 0.0), similarity)
    except Exception:
        logger.exception("text vector search failed")
    try:
        image_query = embedder.embed_image_query(query)
        for chunk_id, similarity in db.vec_ids(
            "vec_image", image_query, pool, max_distance=1.0 - IMAGE_ONLY_FLOOR
        ):
            if chunk_id not in keyword_set and similarity < IMAGE_ONLY_FLOOR:
                continue
            image_ids.append(chunk_id)
            similarities[chunk_id] = max(similarities.get(chunk_id, 0.0), similarity)
    except Exception:
        logger.exception("image vector search failed")
    # A file that contains more of the query outranks a file that shares one
    # word. Filename evidence outranks body text. Vector-only neighbors stay
    # behind anything the words actually appear in.
    tokens = query_tokens(query)
    keyword_ranked = [(chunk_id, 1.0 + 1.0 / (60 + rank)) for rank, chunk_id in enumerate(keyword_ids, start=1)]
    fused_rest = reciprocal_rank_fusion(
        [
            [chunk_id for chunk_id in text_ids if chunk_id not in keyword_set],
            [chunk_id for chunk_id in image_ids if chunk_id not in keyword_set],
        ]
    )
    ranked: list[tuple[int, int, int, float, float, Hit]] = []
    for chunk_id, score in keyword_ranked + fused_rest:
        info = db.chunk_info(chunk_id)
        if info is None:
            continue
        if file_type and info["file_type"] != file_type:
            continue
        if not in_folder(info["path"], folder):
            continue
        page = info["page"]
        similarity = similarities.get(chunk_id)
        if similarity is None:
            similarity = _stored_similarity(db, chunk_id, text_query, image_query)
        name_hits = max(token_hits(str(info["name"]), tokens), token_hits(str(info["path"]), tokens))
        body_hits = token_hits(str(info["text"]), tokens)
        ranked.append(
            (
                name_hits,
                body_hits,
                similarity if similarity is not None else -1.0,
                float(score),
                chunk_id,
                Hit(
                    path=info["path"],
                    name=info["name"],
                    file_type=info["file_type"],
                    page=None if page is None else int(page),
                    snippet=snippet_for(info["text"], query),
                    score=round(float(score), 6),
                    similarity=None if similarity is None else round(similarity, 4),
                ),
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
    hits: list[Hit] = []
    seen: set[str] = set()
    for _name_hits, _body_hits, _similarity, _score, _chunk_id, hit in ranked:
        if hit.path in seen:
            continue
        seen.add(hit.path)
        hits.append(hit)
        if len(hits) >= limit:
            break
    took_ms = int((time.perf_counter() - started) * 1000)
    return SearchResponse(hits, took_ms)

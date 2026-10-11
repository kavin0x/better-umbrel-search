from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Protocol


def l2_normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        result = [0.0] * len(values)
        if result:
            result[0] = 1.0
        return result
    return [value / norm for value in values]


def _token_vector(text: str, dim: int) -> list[float]:
    vector = [0.0] * dim
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    if not tokens:
        tokens = ["empty"]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "little") % dim
        vector[bucket] += 1.0
    return l2_normalize(vector)


class Embedder(Protocol):
    text_dim: int
    image_dim: int

    def embed_passages(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def embed_image(self, path: Path) -> list[float]: ...

    def embed_image_query(self, text: str) -> list[float]: ...


class FakeEmbedder:
    """Deterministic stand-in so tests never download models.

    Tokens share buckets, so a query and a passage that use the same words
    land near each other. Image vectors use the file stem the same way.
    """

    def __init__(self, text_dim: int = 384, image_dim: int = 512) -> None:
        self.text_dim = text_dim
        self.image_dim = image_dim
        self.passage_calls = 0
        self.image_calls = 0

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        self.passage_calls += len(texts)
        return [_token_vector(text, self.text_dim) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return _token_vector(text, self.text_dim)

    def embed_image(self, path: Path) -> list[float]:
        self.image_calls += 1
        return _token_vector(path.stem, self.image_dim)

    def embed_image_query(self, text: str) -> list[float]:
        return _token_vector(text, self.image_dim)


def _as_list(vector: object) -> list[float]:
    return l2_normalize([float(value) for value in vector])  # type: ignore[arg-type]


def _catalog_dim(models: list[dict], name: str) -> int:
    for model in models:
        if model.get("model") == name:
            return int(model["dim"])
    raise ValueError(f"unsupported embedding model: {name}")


class FastEmbedder:
    """CPU ONNX embeddings. Models download into the app data directory once."""

    def __init__(self, cache_dir: Path, text_model: str, image_model: str, clip_text_model: str) -> None:
        from fastembed import ImageEmbedding, TextEmbedding

        self.cache_dir = cache_dir
        self.text_model_name = text_model
        self.image_model_name = image_model
        self.clip_text_model_name = clip_text_model
        self.text_dim = _catalog_dim(TextEmbedding.list_supported_models(), text_model)
        self.image_dim = _catalog_dim(ImageEmbedding.list_supported_models(), image_model)
        clip_dim = _catalog_dim(TextEmbedding.list_supported_models(), clip_text_model)
        if clip_dim != self.image_dim:
            raise ValueError("the photo text model and the photo vision model must use the same dimension")
        self._text = None
        self._clip_text = None
        self._image = None

    def _cache(self) -> str:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return str(self.cache_dir)

    def _text_model(self):
        if self._text is None:
            from fastembed import TextEmbedding

            self._text = TextEmbedding(model_name=self.text_model_name, cache_dir=self._cache())
        return self._text

    def _clip_model(self):
        if self._clip_text is None:
            from fastembed import TextEmbedding

            self._clip_text = TextEmbedding(model_name=self.clip_text_model_name, cache_dir=self._cache())
        return self._clip_text

    def _image_model(self):
        if self._image is None:
            from fastembed import ImageEmbedding

            self._image = ImageEmbedding(model_name=self.image_model_name, cache_dir=self._cache())
        return self._image

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        model = self._text_model()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), 32):
            batch = texts[start : start + 32]
            vectors.extend(_as_list(vector) for vector in model.embed(batch))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        model = self._text_model()
        embed = getattr(model, "query_embed", None)
        if embed is not None:
            return _as_list(next(iter(embed([text]))))
        return _as_list(next(iter(model.embed([f"query: {text}"]))))

    def embed_image(self, path: Path) -> list[float]:
        model = self._image_model()
        return _as_list(next(iter(model.embed([str(path)]))))

    def embed_image_query(self, text: str) -> list[float]:
        model = self._clip_model()
        embed = getattr(model, "query_embed", None)
        if embed is not None:
            return _as_list(next(iter(embed([text]))))
        return _as_list(next(iter(model.embed([text]))))

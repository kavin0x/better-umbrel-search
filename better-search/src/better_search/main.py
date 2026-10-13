from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from strawberry.fastapi import GraphQLRouter

from better_search.config import Settings
from better_search.db import Database
from better_search.embed import Embedder, FakeEmbedder, FastEmbedder
from better_search.indexer import Indexer
from better_search.schema import schema
from better_search.content import safe_library_file

logger = logging.getLogger(__name__)


class AppState:
    def __init__(self, settings: Settings, db: Database, embedder: Embedder, indexer: Indexer) -> None:
        self.settings = settings
        self.db = db
        self.embedder = embedder
        self.indexer = indexer


def _web_dist(settings: Settings) -> Path | None:
    candidates: list[Path] = []
    if settings.web_dist is not None:
        candidates.append(settings.web_dist)
    candidates.append(Path(__file__).resolve().parents[2] / "web" / "dist")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def create_app(
    settings: Settings,
    embedder: Embedder | None = None,
    *,
    start_indexer: bool = False,
) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings.data_path.mkdir(parents=True, exist_ok=True)
    if embedder is None:
        embedder = FastEmbedder(
            settings.data_path / "models",
            settings.text_model,
            settings.image_model,
            settings.clip_text_model,
            threads=settings.index_threads,
        )
    db = Database(settings.data_path / "index.sqlite", embedder.text_dim, embedder.image_dim)
    indexer = Indexer(settings, db, embedder)
    state = AppState(settings, db, embedder, indexer)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_indexer:
            indexer.start()
        yield
        indexer.stop()

    app = FastAPI(title="Better Search", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.search = state

    def context_getter():
        return {"app": state}

    app.include_router(GraphQLRouter(schema, context_getter=context_getter), prefix="/graphql")

    @app.get("/content")
    def content(path: str = Query(min_length=1)):
        target = safe_library_file(settings.library_path, path)
        if target is None:
            raise HTTPException(status_code=404, detail="file not found")
        relative = target.relative_to(settings.library_path.resolve()).as_posix()
        row = state.db.get_file(relative)
        if row is None or row["status"] != "indexed":
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(target)

    dist = _web_dist(settings)
    if dist is not None:
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/")
        def index():
            return FileResponse(dist / "index.html")

    return app


def _build_production_app() -> FastAPI:
    settings = Settings.from_env()
    embedder: Embedder | None = None
    if os.environ.get("EMBEDDER") == "fake":
        embedder = FakeEmbedder()
    return create_app(settings, embedder, start_indexer=True)


app = None if os.environ.get("BETTER_SEARCH_TESTING") == "1" else _build_production_app()

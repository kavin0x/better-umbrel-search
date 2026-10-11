from __future__ import annotations

from enum import Enum

import strawberry
from strawberry.types import Info

from better_search.search import search


@strawberry.enum
class FileType(Enum):
    ALL = "all"
    DOCUMENT = "document"
    IMAGE = "image"


@strawberry.type
class SearchHit:
    path: str
    name: str
    file_type: str
    page: int | None
    snippet: str
    score: float
    similarity: float | None


@strawberry.type
class SearchResult:
    hits: list[SearchHit]
    took_ms: int


@strawberry.type
class IndexStatus:
    state: str
    files_seen: int
    files_indexed: int
    files_skipped: int
    files_failed: int
    last_error: str | None
    priority_folder: str | None


@strawberry.type
class FileDetail:
    path: str
    name: str
    file_type: str
    mime: str | None
    size: int
    status: str


def _app(info: Info):
    context = info.context
    if isinstance(context, dict) and "app" in context:
        return context["app"]
    request = getattr(context, "request", None)
    if request is None and isinstance(context, dict):
        request = context.get("request")
    if request is not None:
        return request.app.state.search
    raise RuntimeError("search context missing")


@strawberry.type
class Query:
    @strawberry.field
    def search(
        self,
        info: Info,
        q: str,
        file_type: FileType = FileType.ALL,
        folder: str | None = None,
        limit: int = 20,
    ) -> SearchResult:
        app = _app(info)
        kind = None if file_type == FileType.ALL else file_type.value
        found = search(
            app.db,
            app.embedder,
            q,
            file_type=kind,
            folder=folder,
            limit=limit,
        )
        return SearchResult(
            hits=[
                SearchHit(
                    path=hit.path,
                    name=hit.name,
                    file_type=hit.file_type,
                    page=hit.page,
                    snippet=hit.snippet,
                    score=hit.score,
                    similarity=hit.similarity,
                )
                for hit in found.hits
            ],
            took_ms=found.took_ms,
        )

    @strawberry.field
    def status(self, info: Info) -> IndexStatus:
        snap = _app(info).indexer.snapshot()
        return IndexStatus(
            state=str(snap["state"]),
            files_seen=int(snap["files_seen"]),
            files_indexed=int(snap["files_indexed"]),
            files_skipped=int(snap["files_skipped"]),
            files_failed=int(snap["files_failed"]),
            last_error=None if snap["last_error"] is None else str(snap["last_error"]),
            priority_folder=None if snap.get("priority_folder") is None else str(snap["priority_folder"]),
        )

    @strawberry.field
    def file(self, info: Info, path: str) -> FileDetail | None:
        app = _app(info)
        from better_search.content import safe_library_file

        if safe_library_file(app.settings.library_path, path) is None and _escape(path):
            return None
        row = app.db.get_file(path.strip("/"))
        if row is None:
            return None
        return FileDetail(
            path=row["path"],
            name=row["name"],
            file_type=row["file_type"],
            mime=row["mime"],
            size=int(row["size"]),
            status=row["status"],
        )


def _escape(path: str) -> bool:
    return path.startswith("/") or path.startswith("\\") or ".." in path.split("/") or "\x00" in path


@strawberry.type
class Mutation:
    @strawberry.mutation
    def prioritize_folder(self, info: Info, path: str) -> str:
        try:
            return _app(info).indexer.prioritize(path)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


schema = strawberry.Schema(query=Query, mutation=Mutation)

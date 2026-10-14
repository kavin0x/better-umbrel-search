from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import xxhash
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from better_search.chunk import TextChunk, chunks_from_pages
from better_search.classify import (
    Decision,
    classify,
    device_predicate,
    iter_library_files,
    library_is_external,
)
from better_search.config import Settings
from better_search.db import Database
from better_search.embed import Embedder
from better_search.extract import (
    default_ocr_image,
    extract_document,
    extract_image,
    limit_tesseract_threads,
)

logger = logging.getLogger(__name__)


def file_hash(path: Path) -> str:
    digest = xxhash.xxh3_64()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relative_to_library(library: Path, path: Path) -> str | None:
    try:
        library_root = library.resolve()
        relative = path.resolve(strict=False).relative_to(library_root)
    except (OSError, ValueError):
        return None
    if ".." in relative.parts:
        return None
    return relative.as_posix()


@dataclass
class _Claim:
    path: Path
    relative: str
    file_type: str
    mime: str | None
    size: int
    mtime: float
    digest: str


@dataclass
class _Extracted:
    claim: _Claim
    captured_at: float | None
    passages: list[str]
    packed: list[TextChunk]
    image: bool


def scan_progress(seen: int, total: int, state: str) -> int | None:
    """Percent of the library walked. Stays under 100 until the scan finishes."""
    if state == "watching":
        return 100 if total > 0 else None
    if state != "indexing" or total <= 0:
        return None
    if seen >= total:
        return 99
    return (seen * 100) // total


class _Handler(FileSystemEventHandler):
    def __init__(self, indexer: Indexer) -> None:
        self.indexer = indexer

    def on_created(self, event) -> None:  # type: ignore[no-untyped-def]
        self.indexer.events.put((Path(event.src_path), bool(event.is_directory), False))

    def on_modified(self, event) -> None:  # type: ignore[no-untyped-def]
        if not event.is_directory:
            self.indexer.events.put((Path(event.src_path), False, False))

    def on_deleted(self, event) -> None:  # type: ignore[no-untyped-def]
        self.indexer.events.put((Path(event.src_path), bool(event.is_directory), True))

    def on_moved(self, event) -> None:  # type: ignore[no-untyped-def]
        self.indexer.events.put((Path(event.src_path), bool(event.is_directory), True))
        self.indexer.events.put((Path(event.dest_path), bool(event.is_directory), False))


class Indexer:
    """Indexes every supported file already on the main disk, then new ones.

    External drives are not entered. A library root that itself sits on a
    removable disk or a USB mount is refused.
    """

    def __init__(self, settings: Settings, db: Database, embedder: Embedder) -> None:
        self.settings = settings
        self.library = settings.library_path
        self.db = db
        self.embedder = embedder
        self.state = "idle"
        self.files_seen = 0
        self.files_indexed = 0
        self.files_skipped = 0
        self.files_failed = 0
        self.last_error: str | None = None
        self.events: queue.Queue[tuple[Path, bool, bool]] = queue.Queue()
        self.scan_token = ""
        self.rescan_interval = 900
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self.current_file: str | None = None
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._observer: Observer | None = None
        self._same = None
        self._priority_folder: str | None = None
        self._priority_lock = threading.Lock()
        self.library_total = 0
        self._visited: set[str] = set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="better-search-index", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._stop_watcher()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._shutdown_pool()

    def halt_indexing(self) -> str:
        """Stop the current scan and later rescans until resume_indexing."""
        self._cancel.set()
        if self.state != "indexing":
            self.state = "stopped"
            self.current_file = None
        return "stopped"

    def resume_indexing(self) -> str:
        self._cancel.clear()
        if self.state == "stopped":
            self.state = "idle"
        return self.state

    def _halted(self) -> bool:
        return self._stop.is_set() or self._cancel.is_set()

    def _settle_cancel(self) -> bool:
        if self._stop.is_set() or not self._cancel.is_set():
            return False
        self.state = "stopped"
        self.current_file = None
        return True

    def snapshot(self) -> dict[str, int | str | None]:
        if self.state == "indexing":
            seen, indexed, skipped, failed = (
                self.files_seen,
                self.files_indexed,
                self.files_skipped,
                self.files_failed,
            )
            total = self.library_total
        else:
            counts = self.db.counts()
            seen = counts["seen"]
            indexed = counts["indexed"]
            skipped = counts["skipped"]
            failed = counts["failed"]
            total = counts["seen"]
        return {
            "state": self.state,
            "files_seen": seen,
            "files_indexed": indexed,
            "files_skipped": skipped,
            "files_failed": failed,
            "last_error": self.last_error,
            "priority_folder": self.priority_folder(),
            "library_total": total,
            "progress": scan_progress(seen, total, self.state),
            "current_file": self.current_file if self.state == "indexing" else None,
        }

    def priority_folder(self) -> str | None:
        with self._priority_lock:
            return self._priority_folder

    def prioritize(self, folder: str) -> str:
        cleaned = _priority_folder(folder)
        root = (self.library / cleaned).resolve()
        try:
            root.relative_to(self.library.resolve())
        except ValueError as exc:
            raise ValueError("folder must stay inside the library") from exc
        if not root.is_dir():
            raise ValueError("folder was not found in the library")
        same = self._same
        if same is None and self.library.exists():
            same = device_predicate(self.library) if self.settings.stay_on_main_device else _always_same
        if same is not None and not same(root):
            raise ValueError("folder is on another disk")
        with self._priority_lock:
            self._priority_folder = cleaned
        return cleaned

    def full_scan(self) -> None:
        if self._settle_cancel():
            return
        if not self.library.exists():
            self.state = "waiting-for-library"
            return
        if self.settings.stay_on_main_device and library_is_external(self.library):
            self.state = "external-drive-blocked"
            self.last_error = "The library is on an external drive, so nothing was indexed."
            return
        self._same = device_predicate(self.library) if self.settings.stay_on_main_device else _always_same
        self.scan_token = uuid.uuid4().hex
        self.state = "indexing"
        self.files_seen = 0
        self.files_indexed = 0
        self.files_skipped = 0
        self.files_failed = 0
        self.last_error = None
        self._visited = set()
        prior = self.db.counts()["seen"]
        self.library_total = prior
        if prior <= 0:
            counted = 0
            for _path in iter_library_files(self.library, self._same):
                counted += 1
                if self._halted():
                    self.library_total = counted
                    self._settle_cancel()
                    return
            self.library_total = counted
        walker = iter_library_files(self.library, self._same)
        window: list[Path] = []
        limit = max(1, self.settings.index_threads)
        while not self._halted():
            self.drain()
            self._drain_priority()
            if self._halted():
                break
            try:
                path = next(walker)
            except StopIteration:
                break
            window.append(path)
            if len(window) >= limit:
                self._index_window(window)
                window = []
        if window and not self._halted():
            self._index_window(window)
        if self._settle_cancel() or self._stop.is_set():
            return
        self.drain()
        removed = self.db.delete_stale(self.scan_token)
        if removed:
            logger.info("removed %s files that left the main disk", removed)
        if self.state == "indexing":
            self.current_file = None
            self.state = "watching"

    def _drain_priority(self) -> None:
        if self._same is None:
            return
        with self._priority_lock:
            folder = self._priority_folder
        if not folder:
            return
        root = self.library / folder
        try:
            if root.is_dir() and self._same(root):
                pending: list[Path] = []
                limit = max(1, self.settings.index_threads)
                for child in iter_library_files(root, self._same):
                    if self._halted():
                        return
                    self.drain()
                    pending.append(child)
                    if len(pending) >= limit:
                        self._index_window(pending)
                        pending = []
                if pending and not self._halted():
                    self._index_window(pending)
        finally:
            with self._priority_lock:
                if self._priority_folder == folder:
                    self._priority_folder = None

    def drain(self) -> None:
        while True:
            try:
                path, is_dir, deleted = self.events.get_nowait()
            except queue.Empty:
                return
            relative = relative_to_library(self.library, path)
            if relative is None:
                continue
            if deleted:
                if is_dir:
                    self.db.delete_prefix(relative)
                else:
                    self.db.delete_path(relative)
                continue
            if is_dir:
                if self._same is not None and not self._same(path):
                    logger.info("skip external mount %s", path)
                    continue
                if self._same is None:
                    continue
                pending: list[Path] = []
                limit = max(1, self.settings.index_threads)
                for child in iter_library_files(path, self._same):
                    if self._halted():
                        return
                    pending.append(child)
                    if len(pending) >= limit:
                        self._index_window(pending)
                        pending = []
                if pending and not self._halted():
                    self._index_window(pending)
                continue
            self._index_window([path])

    def _shutdown_pool(self) -> None:
        pool = self._pool
        self._pool = None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=max(1, self.settings.index_threads),
                thread_name_prefix="better-search-extract",
            )
        return self._pool

    def _index_window(self, paths: list[Path]) -> None:
        """Extract a window of files together, then embed and write them here.

        One thread keeps counters, the current file, and SQLite writes.
        Extraction and OCR use the pool and do not run during embedding, so
        the process stays within ``index_threads`` CPUs.
        """
        if self.settings.index_threads <= 1 or len(paths) <= 1:
            for path in paths:
                if self._halted():
                    return
                self._index_one(path)
            return
        claims: list[_Claim] = []
        for path in paths:
            if self._halted():
                return
            claim = self._prepare_claim(path)
            if claim is not None:
                claims.append(claim)
        if not claims:
            return
        limit_tesseract_threads()
        futures = [self._executor().submit(self._extract, claim) for claim in claims]
        extracted: list[_Extracted] = []
        for claim, future in zip(claims, futures, strict=True):
            try:
                extracted.append(future.result())
            except Exception as exc:
                logger.exception("failed to index %s", claim.relative)
                self._fail(claim.relative, claim.path.name, claim.mime, claim.size, claim.mtime, exc)
        self._embed_and_store(extracted)

    def _index_one(self, path: Path) -> None:
        claim = self._prepare_claim(path)
        if claim is None:
            return
        try:
            self._embed_and_store([self._extract(claim)])
        except Exception as exc:
            logger.exception("failed to index %s", claim.relative)
            self._fail(claim.relative, claim.path.name, claim.mime, claim.size, claim.mtime, exc)

    def _prepare_claim(self, path: Path) -> _Claim | None:
        if self._same is not None and path.exists() and not self._same(path):
            return None
        relative = relative_to_library(self.library, path)
        if relative is None or not path.is_file():
            return None
        if self._halted():
            return None
        self.current_file = relative
        if relative in self._visited:
            self.db.touch_scan(relative, self.scan_token)
            return None
        self._visited.add(relative)
        self.files_seen += 1
        try:
            stat_result = path.stat()
        except OSError as exc:
            self.files_failed += 1
            self.last_error = f"{relative}: {exc}"
            return None
        decision = classify(path, max_bytes=self.settings.max_file_bytes)
        if decision.action == "skip":
            existing = self.db.unchanged(relative, stat_result.st_size, stat_result.st_mtime)
            if existing is not None and existing["status"] == "skipped":
                self.db.touch_scan(relative, self.scan_token)
                self.files_skipped += 1
                return None
            self.db.mark_simple(
                path=relative,
                name=path.name,
                mime=decision.mime,
                size=stat_result.st_size,
                mtime=stat_result.st_mtime,
                status="skipped",
                reason=decision.reason,
                scan_token=self.scan_token,
            )
            self.files_skipped += 1
            return None
        existing = self.db.unchanged(relative, stat_result.st_size, stat_result.st_mtime)
        if existing is not None and existing["status"] == "indexed":
            self.db.touch_scan(relative, self.scan_token)
            self.files_indexed += 1
            return None
        try:
            digest = file_hash(path)
        except OSError as exc:
            self._fail(relative, path.name, decision.mime, stat_result.st_size, stat_result.st_mtime, exc)
            return None
        previous_hash = self.db.hash_of(relative)
        if previous_hash == digest and existing is None:
            row = self.db.get_file(relative)
            if row is not None and row["status"] == "indexed":
                self.db.note_hash_only(relative, stat_result.st_size, stat_result.st_mtime, self.scan_token)
                self.files_indexed += 1
                return None
        return _Claim(
            path=path,
            relative=relative,
            file_type=decision.file_type,
            mime=decision.mime,
            size=stat_result.st_size,
            mtime=stat_result.st_mtime,
            digest=digest,
        )

    def _extract(self, claim: _Claim) -> _Extracted:
        path = claim.path
        if claim.file_type == "image":
            info = extract_image(
                path,
                ocr_enabled=self.settings.ocr_enabled,
                ocr_fn=default_ocr_image,
            )
            body = info.text.strip() or path.stem
            return _Extracted(
                claim=claim,
                captured_at=info.captured_at,
                passages=[f"{path.name}. {body}"],
                packed=[TextChunk(body, None, 0)],
                image=True,
            )
        pieces = extract_document(path, Decision("index", claim.file_type, None, claim.mime))
        pages = [(piece.text, piece.page) for piece in pieces]
        packed = chunks_from_pages(pages)
        if not packed:
            packed = [TextChunk(path.stem, 1, 0)]
        return _Extracted(
            claim=claim,
            captured_at=None,
            passages=[f"{path.name}. {chunk.text}" for chunk in packed],
            packed=packed,
            image=False,
        )

    def _embed_and_store(self, extracted: list[_Extracted]) -> None:
        if not extracted:
            return
        passages: list[str] = []
        spans: list[tuple[int, int]] = []
        for item in extracted:
            start = len(passages)
            passages.extend(item.passages)
            spans.append((start, len(item.passages)))
        try:
            vectors = self.embedder.embed_passages(passages) if passages else []
        except Exception as exc:
            logger.exception("failed to embed a window of %s files", len(extracted))
            for item in extracted:
                claim = item.claim
                self._fail(claim.relative, claim.path.name, claim.mime, claim.size, claim.mtime, exc)
            return
        if len(vectors) != len(passages):
            exc = RuntimeError(f"embedder returned {len(vectors)} vectors for {len(passages)} passages")
            for item in extracted:
                claim = item.claim
                self._fail(claim.relative, claim.path.name, claim.mime, claim.size, claim.mtime, exc)
            return
        for item, (start, count) in zip(extracted, spans, strict=True):
            claim = item.claim
            self.current_file = claim.relative
            try:
                text_vectors = vectors[start : start + count]
                image_vector = self.embedder.embed_image(claim.path) if item.image else None
                chunks = [
                    {
                        "page": chunk.page,
                        "ordinal": chunk.ordinal,
                        "text": chunk.text,
                        "kind": "image" if item.image else "text",
                        "text_vector": vector,
                        "image_vector": image_vector if item.image else None,
                    }
                    for chunk, vector in zip(item.packed, text_vectors, strict=True)
                ]
                self.db.store_document(
                    path=claim.relative,
                    name=claim.path.name,
                    mime=claim.mime,
                    file_type=claim.file_type,
                    size=claim.size,
                    mtime=claim.mtime,
                    content_hash=claim.digest,
                    captured_at=item.captured_at,
                    scan_token=self.scan_token,
                    chunks=chunks,
                )
                self.files_indexed += 1
            except Exception as exc:
                logger.exception("failed to index %s", claim.relative)
                self._fail(claim.relative, claim.path.name, claim.mime, claim.size, claim.mtime, exc)

    def _fail(self, relative: str, name: str, mime: str | None, size: int, mtime: float, exc: Exception) -> None:
        self.files_failed += 1
        self.last_error = f"{relative}: {exc}"
        self.db.mark_simple(
            path=relative,
            name=name,
            mime=mime,
            size=size,
            mtime=mtime,
            status="failed",
            reason=str(exc),
            scan_token=self.scan_token,
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("index loop failed")
                self.last_error = "Indexing hit an unexpected error and will retry."
                self._stop.wait(1)

    def _tick(self) -> None:
        if self._cancel.is_set():
            self.state = "stopped"
            self.current_file = None
            self._stop.wait(0.5)
            return
        if not self.library.exists():
            self.state = "waiting-for-library"
            self._stop.wait(2)
            return
        if self.settings.stay_on_main_device and library_is_external(self.library):
            self.state = "external-drive-blocked"
            self.last_error = "The library is on an external drive, so nothing was indexed."
            self._stop.wait(15)
            return
        self._ensure_watcher()
        self.full_scan()
        if self._cancel.is_set():
            self.state = "stopped"
            self.current_file = None
            return
        if self.state == "external-drive-blocked":
            self._stop.wait(15)
            return
        deadline = time.monotonic() + self.rescan_interval
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self._cancel.is_set():
                break
            self.state = "watching"
            self.drain()
            self._drain_priority()
            self._stop.wait(0.5)

    def _ensure_watcher(self) -> None:
        if self._observer is not None:
            return
        try:
            observer = Observer()
            observer.schedule(_Handler(self), str(self.library), recursive=True)
            observer.start()
        except Exception:
            logger.warning("filesystem watch unavailable; scanning every minute", exc_info=True)
            self.rescan_interval = 60
            return
        self._observer = observer
        self.rescan_interval = 900

    def _stop_watcher(self) -> None:
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=3)
        self._observer = None


def _priority_folder(folder: str) -> str:
    cleaned = folder.strip().strip("/")
    if not cleaned or "\x00" in cleaned:
        raise ValueError("folder must stay inside the library")
    parts = cleaned.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("folder must stay inside the library")
    return cleaned


def _always_same(_path: Path) -> bool:
    try:
        return not _path.is_symlink()
    except OSError:
        return False

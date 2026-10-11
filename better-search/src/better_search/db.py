from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import sqlite_vec
from sqlite_vec import serialize_float32


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)
    return connection


class Database:
    def __init__(self, path: Path, text_dim: int, image_dim: int) -> None:
        self.path = path
        self.text_dim = text_dim
        self.image_dim = image_dim
        self._lock = threading.Lock()
        self._write = connect(path)
        self._migrate()

    def _migrate(self) -> None:
        connection = self._write
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                mime TEXT,
                file_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                content_hash TEXT,
                status TEXT NOT NULL,
                skip_reason TEXT,
                indexed_at REAL,
                captured_at REAL,
                scan_token TEXT
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                page INTEGER,
                ordinal INTEGER NOT NULL,
                text TEXT NOT NULL,
                kind TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text,
                name,
                path,
                tokenize='porter unicode61'
            )
            """
        )
        stored_text = self._meta("text_dim")
        stored_image = self._meta("image_dim")
        if stored_text != str(self.text_dim) or stored_image != str(self.image_dim):
            connection.execute("DROP TABLE IF EXISTS vec_text")
            connection.execute("DROP TABLE IF EXISTS vec_image")
            connection.execute("DELETE FROM chunks_fts")
            connection.execute("DELETE FROM chunks")
            connection.execute("UPDATE files SET content_hash = NULL, status = 'pending' WHERE status = 'indexed'")
        connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_text USING vec0(embedding float[{self.text_dim}] distance_metric=cosine)"
        )
        connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_image USING vec0(embedding float[{self.image_dim}] distance_metric=cosine)"
        )
        self._set_meta("text_dim", str(self.text_dim))
        self._set_meta("image_dim", str(self.image_dim))
        connection.commit()

    def _meta(self, key: str) -> str | None:
        row = self._write.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _set_meta(self, key: str, value: str) -> None:
        self._write.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def read(self) -> sqlite3.Connection:
        return connect(self.path)

    def _fetch(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        connection = self.read()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def get_file(self, path: str) -> sqlite3.Row | None:
        rows = self._fetch("SELECT * FROM files WHERE path = ?", (path,))
        return None if not rows else rows[0]

    def counts(self) -> dict[str, int]:
        row = self._fetch(
            """
            SELECT
                COUNT(*) AS seen,
                COALESCE(SUM(status = 'indexed'), 0) AS indexed,
                COALESCE(SUM(status = 'skipped'), 0) AS skipped,
                COALESCE(SUM(status = 'failed'), 0) AS failed
            FROM files
            """
        )[0]
        return {
            "seen": int(row["seen"]),
            "indexed": int(row["indexed"]),
            "skipped": int(row["skipped"]),
            "failed": int(row["failed"]),
        }

    def unchanged(self, path: str, size: int, mtime: float) -> sqlite3.Row | None:
        row = self.get_file(path)
        if row is None:
            return None
        if int(row["size"]) == size and float(row["mtime"]) == mtime and row["status"] in {"indexed", "skipped"}:
            return row
        return None

    def touch_scan(self, path: str, scan_token: str) -> None:
        with self._lock:
            self._write.execute("UPDATE files SET scan_token = ? WHERE path = ?", (scan_token, path))
            self._write.commit()

    def mark_simple(
        self,
        *,
        path: str,
        name: str,
        mime: str | None,
        size: int,
        mtime: float,
        status: str,
        reason: str | None,
        scan_token: str,
        content_hash: str | None = None,
    ) -> None:
        with self._lock:
            self._delete_chunks_for_path(path)
            self._write.execute(
                """
                INSERT INTO files (
                    path, name, mime, file_type, size, mtime, content_hash, status,
                    skip_reason, indexed_at, captured_at, scan_token
                ) VALUES (?, ?, ?, 'skipped', ?, ?, ?, ?, ?, NULL, NULL, ?)
                ON CONFLICT(path) DO UPDATE SET
                    name = excluded.name,
                    mime = excluded.mime,
                    file_type = 'skipped',
                    size = excluded.size,
                    mtime = excluded.mtime,
                    content_hash = excluded.content_hash,
                    status = excluded.status,
                    skip_reason = excluded.skip_reason,
                    indexed_at = NULL,
                    captured_at = NULL,
                    scan_token = excluded.scan_token
                """,
                (path, name, mime, size, mtime, content_hash, status, reason, scan_token),
            )
            self._write.commit()

    def store_document(
        self,
        *,
        path: str,
        name: str,
        mime: str | None,
        file_type: str,
        size: int,
        mtime: float,
        content_hash: str,
        captured_at: float | None,
        scan_token: str,
        chunks: list[dict[str, Any]],
    ) -> None:
        with self._lock:
            self._delete_chunks_for_path(path)
            self._write.execute(
                """
                INSERT INTO files (
                    path, name, mime, file_type, size, mtime, content_hash, status,
                    skip_reason, indexed_at, captured_at, scan_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'indexed', NULL, strftime('%s','now'), ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    name = excluded.name,
                    mime = excluded.mime,
                    file_type = excluded.file_type,
                    size = excluded.size,
                    mtime = excluded.mtime,
                    content_hash = excluded.content_hash,
                    status = 'indexed',
                    skip_reason = NULL,
                    indexed_at = strftime('%s','now'),
                    captured_at = excluded.captured_at,
                    scan_token = excluded.scan_token
                """,
                (path, name, mime, file_type, size, mtime, content_hash, captured_at, scan_token),
            )
            file_id = self._write.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()["id"]
            for chunk in chunks:
                cursor = self._write.execute(
                    """
                    INSERT INTO chunks (file_id, page, ordinal, text, kind)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (file_id, chunk.get("page"), chunk["ordinal"], chunk["text"], chunk["kind"]),
                )
                chunk_id = int(cursor.lastrowid)
                self._write.execute(
                    "INSERT INTO chunks_fts(rowid, text, name, path) VALUES (?, ?, ?, ?)",
                    (chunk_id, chunk["text"], name, path),
                )
                if chunk.get("text_vector") is not None:
                    self._write.execute(
                        "INSERT INTO vec_text(rowid, embedding) VALUES (?, ?)",
                        (chunk_id, serialize_float32(chunk["text_vector"])),
                    )
                if chunk.get("image_vector") is not None:
                    self._write.execute(
                        "INSERT INTO vec_image(rowid, embedding) VALUES (?, ?)",
                        (chunk_id, serialize_float32(chunk["image_vector"])),
                    )
            self._write.commit()

    def note_hash_only(self, path: str, size: int, mtime: float, scan_token: str) -> None:
        with self._lock:
            self._write.execute(
                "UPDATE files SET size = ?, mtime = ?, scan_token = ? WHERE path = ?",
                (size, mtime, scan_token, path),
            )
            self._write.commit()

    def delete_path(self, path: str) -> None:
        with self._lock:
            self._delete_chunks_for_path(path)
            self._write.execute("DELETE FROM files WHERE path = ?", (path,))
            self._write.commit()

    def delete_stale(self, scan_token: str) -> int:
        with self._lock:
            rows = self._write.execute(
                "SELECT path FROM files WHERE scan_token IS NULL OR scan_token != ?",
                (scan_token,),
            ).fetchall()
            for row in rows:
                self._delete_chunks_for_path(row["path"])
                self._write.execute("DELETE FROM files WHERE path = ?", (row["path"],))
            self._write.commit()
            return len(rows)

    def hash_of(self, path: str) -> str | None:
        row = self.get_file(path)
        if row is None or row["content_hash"] is None:
            return None
        return str(row["content_hash"])

    def _delete_chunks_for_path(self, path: str) -> None:
        row = self._write.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
        if row is None:
            return
        chunk_ids = self._write.execute("SELECT id FROM chunks WHERE file_id = ?", (row["id"],)).fetchall()
        for chunk in chunk_ids:
            chunk_id = chunk["id"]
            self._write.execute("DELETE FROM vec_text WHERE rowid = ?", (chunk_id,))
            self._write.execute("DELETE FROM vec_image WHERE rowid = ?", (chunk_id,))
            self._write.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
        self._write.execute("DELETE FROM chunks WHERE file_id = ?", (row["id"],))

    def delete_prefix(self, prefix: str) -> None:
        prefix = prefix.strip("/")
        if not prefix:
            return
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            rows = self._write.execute(
                "SELECT path FROM files WHERE path = ? OR path LIKE ? ESCAPE '\\'",
                (prefix, escaped + "/%"),
            ).fetchall()
            for row in rows:
                self._delete_chunks_for_path(row["path"])
                self._write.execute("DELETE FROM files WHERE path = ?", (row["path"],))
            self._write.commit()

    def fts_ids(self, match: str, limit: int) -> list[int]:
        connection = self.read()
        try:
            try:
                rows = connection.execute(
                    """
                    SELECT rowid FROM chunks_fts
                    WHERE chunks_fts MATCH ?
                    LIMIT ?
                    """,
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        finally:
            connection.close()
        return [int(row["rowid"]) for row in rows]

    def vec_ids(self, table: str, vector: list[float], limit: int, max_distance: float = 0.75) -> list[int]:
        if table not in {"vec_text", "vec_image"}:
            raise ValueError(table)
        blob = serialize_float32(vector)
        connection = self.read()
        try:
            rows = connection.execute(
                f"SELECT rowid, distance FROM {table} WHERE embedding MATCH ? AND k = ?",
                (blob, limit),
            ).fetchall()
        finally:
            connection.close()
        return [int(row["rowid"]) for row in rows if float(row["distance"]) <= max_distance]

    def chunk_info(self, chunk_id: int) -> sqlite3.Row | None:
        rows = self._fetch(
            """
            SELECT c.id, c.page, c.text, c.kind, f.path, f.name, f.file_type, f.mime, f.size, f.status
            FROM chunks c
            JOIN files f ON f.id = c.file_id
            WHERE c.id = ?
            """,
            (chunk_id,),
        )
        return None if not rows else rows[0]

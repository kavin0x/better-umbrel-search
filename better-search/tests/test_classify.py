from pathlib import Path

from better_search.classify import classify, iter_library_files, looks_external


def test_junk_video_archive_and_documents(tmp_path: Path) -> None:
    library = tmp_path / "library"
    (library / "System Volume Information").mkdir(parents=True)
    (library / "System Volume Information" / "notes.txt").write_text("secret", encoding="utf-8")
    (library / ".DS_Store").write_bytes(b"junk")
    (library / "Thumbs.db").write_bytes(b"junk")
    (library / "movie.mp4").write_bytes(b"not-a-video")
    (library / "bundle.zip").write_bytes(b"PK")
    (library / "notes.txt").write_text("hello", encoding="utf-8")
    (library / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    (library / "app.exe").write_bytes(b"MZ")

    assert classify(library / ".DS_Store", max_bytes=1000).reason == "junk-name"
    assert classify(library / "Thumbs.db", max_bytes=1000).reason == "junk-name"
    assert classify(library / "System Volume Information" / "notes.txt", max_bytes=1000).reason == "junk-directory"
    assert classify(library / "movie.mp4", max_bytes=1000).reason == "video"
    assert classify(library / "bundle.zip", max_bytes=1000).reason == "archive"
    assert classify(library / "app.exe", max_bytes=1000).reason == "binary"
    assert classify(library / "notes.txt", max_bytes=1000).file_type == "document"
    assert classify(library / "photo.jpg", max_bytes=1000).file_type == "image"


def test_extensionless_file_uses_mime(tmp_path: Path) -> None:
    path = tmp_path / "noext"
    path.write_bytes(b"%PDF-1.4")
    decision = classify(path, max_bytes=1000, mime_of=lambda _path: "application/pdf")
    assert decision.action == "index"
    assert decision.file_type == "document"


def test_generic_zip_mime_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "blob"
    path.write_bytes(b"PK")
    decision = classify(path, max_bytes=1000, mime_of=lambda _path: "application/zip")
    assert decision.reason == "archive"


def test_too_large_and_appledouble(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("hello", encoding="utf-8")
    assert classify(big, max_bytes=1).reason == "too-large"
    hidden = tmp_path / "._notes.txt"
    hidden.write_text("resource fork", encoding="utf-8")
    assert classify(hidden, max_bytes=1000).reason == "junk-name"


def test_external_mount_is_not_walked(tmp_path: Path) -> None:
    root = tmp_path / "library"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "main.txt").write_text("on the main disk", encoding="utf-8")
    (root / "usb").mkdir()
    (root / "usb" / "external.txt").write_text("leave this", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("git", encoding="utf-8")

    def same_device(path):
        return "usb" not in path.parts

    found = sorted(path.name for path in iter_library_files(root, same_device))
    assert found == ["main.txt"]


def test_home_noise_is_excluded(tmp_path: Path) -> None:
    root = tmp_path / "library"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "note.txt").write_text("keep", encoding="utf-8")
    skipped = (
        "node_modules",
        ".venv",
        ".cache",
        ".config",
        "@eaDir",
        "#recycle",
        "Family.photoslibrary",
        "Mail.app",
    )
    for name in skipped:
        folder = root / name
        folder.mkdir()
        (folder / "secret.txt").write_text("leave this", encoding="utf-8")
    found = sorted(path.name for path in iter_library_files(root, lambda _path: True))
    assert found == ["note.txt"]
    nested = root / ".venv" / "lib.txt"
    assert classify(nested, max_bytes=1000).reason == "junk-directory"
    song = root / "song.mp3"
    song.write_bytes(b"ID3")
    assert classify(song, max_bytes=1000).reason == "audio"
    database = root / "cache.sqlite"
    database.write_bytes(b"SQLite format 3")
    assert classify(database, max_bytes=1000).reason == "binary"
    office_lock = root / "~$budget.xlsx"
    office_lock.write_bytes(b"lock")
    assert classify(office_lock, max_bytes=1000).reason == "junk-name"


def test_external_drive_signals() -> None:
    assert looks_external(removable="1\n", mount_source=None)
    assert looks_external(removable="0", mount_source="/dev/sdb1") is False
    assert looks_external(removable=None, mount_source="/media/usb/backup")
    assert looks_external(removable=None, mount_source="/dev/nvme0n1p2") is False

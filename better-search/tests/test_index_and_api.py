import time

from better_search.indexer import relative_to_library, scan_progress


def test_scan_progress_stays_under_100_until_the_walk_finishes() -> None:
    assert scan_progress(0, 10, "indexing") == 0
    assert scan_progress(4, 10, "indexing") == 40
    assert scan_progress(10, 10, "indexing") == 99
    assert scan_progress(12, 10, "indexing") == 99
    assert scan_progress(10, 10, "watching") == 100
    assert scan_progress(0, 0, "indexing") is None


def test_search_reports_similarity(stack) -> None:
    response = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "roof leak") { hits { path similarity } } }'},
    )
    body = response.json()
    assert response.status_code == 200, body
    assert "errors" not in body
    hit = body["data"]["search"]["hits"][0]
    assert hit["path"] == "notes/roof.txt"
    assert hit["similarity"] > 0
    assert hit["similarity"] <= 1
    db = stack.app.state.search.db
    chunk_id = int(db._fetch("SELECT id FROM chunks LIMIT 1")[0]["id"])
    vector = db.embedding("vec_text", chunk_id)
    assert vector is not None
    assert len(vector) == stack.embedder.text_dim


def test_priority_folder_is_indexed_before_the_rest(stack, monkeypatch) -> None:
    from better_search.indexer import iter_library_files

    late = stack.library / "aaa"
    soon = stack.library / "zzz"
    late.mkdir()
    soon.mkdir()
    (late / "late.txt").write_text("late quartz note", encoding="utf-8")
    (soon / "soon.txt").write_text("soon quartz note", encoding="utf-8")

    def ordered(root, same):
        if root.resolve() == stack.library.resolve():
            yield from iter_library_files(late, same)
            yield from iter_library_files(soon, same)
            return
        yield from iter_library_files(root, same)

    monkeypatch.setattr("better_search.indexer.iter_library_files", ordered)
    order: list[str] = []
    tokens: list[str] = []
    original = stack.indexer._index_one

    def wrapped(path) -> None:
        tokens.append(stack.indexer.scan_token)
        order.append(path.name)
        original(path)

    stack.indexer.prioritize("zzz")
    stack.indexer._index_one = wrapped
    stack.indexer.full_scan()
    assert order.index("soon.txt") < order.index("late.txt")
    assert tokens[0]
    assert len(set(tokens)) == 1


def test_priority_during_scan_does_not_reset(stack, monkeypatch) -> None:
    from better_search.indexer import iter_library_files

    late = stack.library / "aaa"
    soon = stack.library / "zzz"
    late.mkdir()
    soon.mkdir()
    (late / "late.txt").write_text("late quartz note", encoding="utf-8")
    (soon / "soon.txt").write_text("soon quartz note", encoding="utf-8")

    def ordered(root, same):
        if root.resolve() == stack.library.resolve():
            yield from iter_library_files(late, same)
            yield from iter_library_files(soon, same)
            return
        yield from iter_library_files(root, same)

    monkeypatch.setattr("better_search.indexer.iter_library_files", ordered)
    order: list[str] = []
    tokens: list[str] = []
    seen_counts: list[int] = []
    original = stack.indexer._index_one

    def wrapped(path) -> None:
        if not tokens:
            stack.indexer.prioritize("zzz")
        tokens.append(stack.indexer.scan_token)
        seen_counts.append(stack.indexer.files_seen)
        order.append(path.name)
        original(path)

    stack.indexer._index_one = wrapped
    stack.indexer.full_scan()
    assert order[0] == "late.txt"
    assert order[1] == "soon.txt"
    assert seen_counts[1] >= 1
    assert len(set(tokens)) == 1


def test_prioritize_folder_mutation(stack) -> None:
    accepted = stack.client.post(
        "/graphql",
        json={
            "query": "mutation ($path: String!) { prioritizeFolder(path: $path) }",
            "variables": {"path": "notes"},
        },
    ).json()
    assert accepted["data"]["prioritizeFolder"] == "notes"
    status = stack.client.post(
        "/graphql",
        json={"query": "{ status { priorityFolder } }"},
    ).json()["data"]["status"]
    assert status["priorityFolder"] == "notes"
    escaped = stack.client.post(
        "/graphql",
        json={
            "query": "mutation ($path: String!) { prioritizeFolder(path: $path) }",
            "variables": {"path": "../secret"},
        },
    ).json()
    assert escaped["errors"]
    stack.rescan()
    cleared = stack.client.post(
        "/graphql",
        json={"query": "{ status { priorityFolder } }"},
    ).json()["data"]["status"]
    assert cleared["priorityFolder"] is None


def test_indexes_existing_files_and_skips_junk(stack) -> None:
    counts = stack.indexer.snapshot()
    assert counts["files_indexed"] == 2
    assert counts["files_skipped"] == 0
    response = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "roof leak") { hits { path name fileType snippet page } } }'},
    )
    body = response.json()
    assert response.status_code == 200, body
    assert "errors" not in body
    hits = body["data"]["search"]["hits"]
    assert hits[0]["path"] == "notes/roof.txt"
    assert "roof leak" in hits[0]["snippet"]
    assert hits[0]["fileType"] == "document"


def test_second_scan_does_not_reembed(stack) -> None:
    calls = stack.embedder.passage_calls
    stack.rescan()
    assert stack.embedder.passage_calls == calls


def test_new_file_is_indexed_on_the_next_scan(stack) -> None:
    (stack.library / "notes" / "later.txt").write_text(
        "brand new contract for the porch",
        encoding="utf-8",
    )
    stack.rescan()
    response = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "porch contract") { hits { name } } }'},
    )
    names = [hit["name"] for hit in response.json()["data"]["search"]["hits"]]
    assert "later.txt" in names


def test_watcher_drain_indexes_a_new_file(stack) -> None:
    path = stack.library / "arrived.txt"
    path.write_text("arrived later on the main disk", encoding="utf-8")
    stack.indexer.events.put((path, False, False))
    stack.indexer.drain()
    response = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "arrived later") { hits { name } } }'},
    )
    names = [hit["name"] for hit in response.json()["data"]["search"]["hits"]]
    assert "arrived.txt" in names


def test_deleted_file_leaves_the_index(stack) -> None:
    (stack.library / "notes" / "roof.txt").unlink()
    stack.rescan()
    response = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "roof leak") { hits { path } } }'},
    )
    assert response.json()["data"]["search"]["hits"] == []


def test_changed_file_replaces_old_text(stack) -> None:
    note = stack.library / "notes" / "roof.txt"
    note.write_text("the porch was painted blue", encoding="utf-8")
    stack.rescan()
    found = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "painted porch") { hits { snippet } } }'},
    ).json()["data"]["search"]["hits"]
    assert found
    assert "painted" in found[0]["snippet"]


def test_photo_search_and_type_filter(stack) -> None:
    photos = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "red bike", fileType: IMAGE) { hits { name fileType } } }'},
    ).json()["data"]["search"]["hits"]
    assert photos
    assert photos[0]["name"] == "red-bike.png"
    assert photos[0]["fileType"] == "image"
    documents = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "red bike", fileType: DOCUMENT) { hits { name } } }'},
    ).json()["data"]["search"]["hits"]
    assert all(hit["name"] != "red-bike.png" for hit in documents)


def test_scan_reports_library_progress(stack) -> None:
    status = stack.client.post(
        "/graphql",
        json={"query": "{ status { state libraryTotal progress filesSeen } }"},
    ).json()["data"]["status"]
    assert status["state"] == "watching"
    assert status["libraryTotal"] == status["filesSeen"] == 2
    assert status["progress"] == 100


def test_new_file_is_indexed_during_the_scan(stack) -> None:
    path = stack.library / "midway.txt"
    path.write_text("midway arrival on the main disk", encoding="utf-8")
    original = stack.indexer._index_one
    queued = False
    order: list[str] = []

    def wrapped(candidate) -> None:
        nonlocal queued
        if not queued:
            stack.indexer.events.put((path, False, False))
            queued = True
        order.append(candidate.name)
        original(candidate)

    stack.indexer._index_one = wrapped
    stack.indexer.full_scan()
    assert "midway.txt" in order
    assert order.index("midway.txt") < len(order) - 1
    response = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "midway arrival") { hits { name } } }'},
    )
    names = [hit["name"] for hit in response.json()["data"]["search"]["hits"]]
    assert "midway.txt" in names


def test_status_and_file_query(stack) -> None:
    status = stack.client.post(
        "/graphql",
        json={"query": "{ status { state filesIndexed filesSkipped filesFailed } }"},
    ).json()["data"]["status"]
    assert status["state"] == "watching"
    assert status["filesIndexed"] == 2
    detail = stack.client.post(
        "/graphql",
        json={"query": '{ file(path: "notes/roof.txt") { name status } }'},
    ).json()["data"]["file"]
    assert detail["name"] == "roof.txt"
    assert detail["status"] == "indexed"
    escaped = stack.client.post(
        "/graphql",
        json={"query": '{ file(path: "../secret.txt") { name } }'},
    ).json()["data"]["file"]
    assert escaped is None


def test_content_route_stays_inside_the_library(stack) -> None:
    ok = stack.client.get("/content", params={"path": "notes/roof.txt"})
    assert ok.status_code == 200
    assert b"roof leak" in ok.content
    escaped = stack.client.get("/content", params={"path": "../data/index.sqlite"})
    assert escaped.status_code == 404
    absolute = stack.client.get("/content", params={"path": "/etc/passwd"})
    assert absolute.status_code == 404


def test_external_library_is_not_indexed(stack, monkeypatch) -> None:
    monkeypatch.setattr("better_search.indexer.library_is_external", lambda _path: True)
    (stack.library / "extra.txt").write_text("should stay out", encoding="utf-8")
    stack.rescan()
    assert stack.indexer.state == "external-drive-blocked"
    response = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "should stay out") { hits { name } } }'},
    )
    names = [hit["name"] for hit in response.json()["data"]["search"]["hits"]]
    assert "extra.txt" not in names
    assert stack.app.state.search.db.get_file("extra.txt") is None


def test_stop_names_the_current_file_and_leaves_the_rest(stack) -> None:
    (stack.library / "held-back.txt").write_text("held back from the scan", encoding="utf-8")
    (stack.library / "also-held.txt").write_text("also held back", encoding="utf-8")
    calls: list[str] = []
    original = stack.indexer._index_one

    def wrapped(path) -> None:
        original(path)
        relative = relative_to_library(stack.library, path)
        assert stack.indexer.current_file == relative
        body = stack.client.post(
            "/graphql",
            json={"query": "{ status { state currentFile } }"},
        ).json()
        assert body["data"]["status"]["state"] == "indexing"
        assert body["data"]["status"]["currentFile"] == relative
        calls.append(path.name)
        stack.indexer.halt_indexing()

    stack.indexer._index_one = wrapped
    stack.indexer.full_scan()
    assert len(calls) == 1
    assert stack.indexer.state == "stopped"
    assert stack.indexer.current_file is None
    db = stack.app.state.search.db
    assert db.get_file("notes/roof.txt") is not None
    missed = [name for name in ("held-back.txt", "also-held.txt") if name not in calls]
    assert missed
    for name in missed:
        assert db.get_file(name) is None
    stack.indexer._index_one = original
    resumed = stack.client.post("/graphql", json={"query": "mutation { resumeIndexing }"}).json()
    assert resumed["data"]["resumeIndexing"] == "idle"
    stack.rescan()
    assert stack.indexer.state == "watching"
    assert db.get_file("held-back.txt")["status"] == "indexed"
    assert db.get_file("also-held.txt")["status"] == "indexed"


def test_stop_and_resume_mutations_hold_the_walker(stack) -> None:
    stopped = stack.client.post("/graphql", json={"query": "mutation { stopIndexing }"}).json()
    assert stopped["data"]["stopIndexing"] == "stopped"
    status = stack.client.post(
        "/graphql",
        json={"query": "{ status { state currentFile } }"},
    ).json()["data"]["status"]
    assert status["state"] == "stopped"
    assert status["currentFile"] is None
    (stack.library / "after-stop.txt").write_text("wait until resume", encoding="utf-8")
    stack.rescan()
    assert stack.app.state.search.db.get_file("after-stop.txt") is None
    assert stack.indexer.state == "stopped"
    stack.client.post("/graphql", json={"query": "mutation { resumeIndexing }"})
    stack.rescan()
    assert stack.app.state.search.db.get_file("after-stop.txt")["status"] == "indexed"


def test_indexer_thread_stays_stopped_until_resume(stack) -> None:
    stack.indexer.rescan_interval = 60
    stack.indexer.halt_indexing()
    stack.indexer.start()
    try:
        deadline = time.monotonic() + 2
        while stack.indexer.state != "stopped" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert stack.indexer.state == "stopped"
        stack.indexer.resume_indexing()
        deadline = time.monotonic() + 3
        while stack.indexer.state != "watching" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert stack.indexer.state == "watching"
    finally:
        stack.indexer.stop()


def test_skipped_files_are_recorded(stack) -> None:
    (stack.library / "movie.mp4").write_bytes(b"video")
    (stack.library / ".DS_Store").write_bytes(b"junk")
    (stack.library / "photos.zip").write_bytes(b"PK")
    stack.rescan()
    status = stack.client.post(
        "/graphql",
        json={"query": "{ status { filesSkipped filesIndexed } }"},
    ).json()["data"]["status"]
    assert status["filesSkipped"] == 3
    assert status["filesIndexed"] == 2

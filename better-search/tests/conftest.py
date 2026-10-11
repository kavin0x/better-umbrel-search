import os

os.environ["BETTER_SEARCH_TESTING"] = "1"

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from better_search.config import Settings
from better_search.embed import FakeEmbedder
from better_search.main import create_app


@dataclass
class Stack:
    app: object
    library: Path
    data: Path
    embedder: FakeEmbedder
    client: TestClient

    @property
    def indexer(self):
        return self.app.state.search.indexer

    def rescan(self) -> None:
        self.indexer.full_scan()


@pytest.fixture
def stack(tmp_path: Path) -> Stack:
    library = tmp_path / "library"
    data = tmp_path / "data"
    (library / "notes").mkdir(parents=True)
    (library / "notes" / "roof.txt").write_text(
        "The roof leak invoice is in the kitchen drawer.",
        encoding="utf-8",
    )
    image = Image.new("RGB", (12, 12), (180, 40, 40))
    image.save(library / "red-bike.png")
    settings = Settings(library_path=library, data_path=data, ocr_enabled=False)
    embedder = FakeEmbedder()
    app = create_app(settings, embedder, start_indexer=False)
    app.state.search.indexer.full_scan()
    client = TestClient(app)
    return Stack(app=app, library=library, data=data, embedder=embedder, client=client)

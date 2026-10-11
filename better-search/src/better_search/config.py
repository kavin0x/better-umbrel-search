from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    library_path: Path
    data_path: Path
    max_file_bytes: int = 100 * 1024 * 1024
    ocr_enabled: bool = True
    stay_on_main_device: bool = True
    text_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    image_model: str = "Qdrant/clip-ViT-B-32-vision"
    clip_text_model: str = "Qdrant/clip-ViT-B-32-text"
    host: str = "0.0.0.0"
    port: int = 8787
    web_dist: Path | None = None

    @classmethod
    def from_env(cls) -> Settings:
        library = Path(os.environ.get("LIBRARY_PATH", "/library"))
        data = Path(os.environ.get("DATA_PATH", "/data"))
        web = os.environ.get("WEB_DIST")
        return cls(
            library_path=library,
            data_path=data,
            max_file_bytes=int(os.environ.get("MAX_FILE_BYTES", str(100 * 1024 * 1024))),
            ocr_enabled=os.environ.get("OCR_ENABLED", "1") not in {"0", "false", "False"},
            stay_on_main_device=os.environ.get("STAY_ON_MAIN_DEVICE", "1") not in {"0", "false", "False"},
            text_model=os.environ.get("TEXT_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"),
            image_model=os.environ.get("IMAGE_MODEL", "Qdrant/clip-ViT-B-32-vision"),
            clip_text_model=os.environ.get("CLIP_TEXT_MODEL", "Qdrant/clip-ViT-B-32-text"),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8787")),
            web_dist=Path(web) if web else None,
        )

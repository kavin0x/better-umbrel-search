from better_search.config import Settings
from better_search.embed import qwen_query


def test_qwen_query_uses_the_instruct_prefix() -> None:
    text = qwen_query("roof leak")
    assert text == (
        "Instruct: Given a file search query, retrieve relevant passages from personal documents and notes\n"
        "Query:roof leak"
    )


def test_default_models_are_qwen_and_jina_clip(monkeypatch) -> None:
    for name in ("TEXT_MODEL", "IMAGE_MODEL", "CLIP_TEXT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LIBRARY_PATH", "/library")
    monkeypatch.setenv("DATA_PATH", "/data")
    settings = Settings.from_env()
    assert settings.text_model == "Qwen/Qwen3-Embedding-0.6B-Q"
    assert settings.image_model == "jinaai/jina-clip-v1"
    assert settings.clip_text_model == "jinaai/jina-clip-v1"

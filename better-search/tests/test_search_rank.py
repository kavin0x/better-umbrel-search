from PIL import Image

from better_search.search import snippet_for


def test_unlabeled_photo_is_not_a_guess_for_a_name(stack) -> None:
    Image.new("RGB", (8, 8), (200, 180, 160)).save(stack.library / "baby.jpg")
    (stack.library / "notes" / "grades.htm").write_text(
        "final grades were posted on tuesday",
        encoding="utf-8",
    )
    stack.rescan()
    hits = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "grandmas") { hits { name } } }'},
    ).json()["data"]["search"]["hits"]
    names = [hit["name"] for hit in hits]
    assert "baby.jpg" not in names
    assert "grades.htm" not in names


def test_plural_query_finds_the_named_photo_first(stack) -> None:
    Image.new("RGB", (8, 8), (20, 20, 20)).save(stack.library / "grandma.jpg")
    Image.new("RGB", (8, 8), (200, 180, 160)).save(stack.library / "baby.jpg")
    (stack.library / "notes" / "stranger.txt").write_text(
        "a middle aged woman holding a baby",
        encoding="utf-8",
    )
    stack.rescan()
    hits = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "grandmas") { hits { name } } }'},
    ).json()["data"]["search"]["hits"]
    assert hits
    assert hits[0]["name"] == "grandma.jpg"


def test_author_sentence_ranks_the_paper_above_a_resume(stack) -> None:
    (stack.library / "nidhi-resume.txt").write_text(
        "Nidhi Sharma resume. Education, experience, and skills.",
        encoding="utf-8",
    )
    (stack.library / "method-notes.txt").write_text(
        "Anup Shah describes a method for tracking objects across frames.",
        encoding="utf-8",
    )
    stack.rescan()
    hits = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "research paper made by anup shah") { hits { name } } }'},
    ).json()["data"]["search"]["hits"]
    names = [hit["name"] for hit in hits]
    assert names[0] == "method-notes.txt"
    if "nidhi-resume.txt" in names:
        assert names.index("method-notes.txt") < names.index("nidhi-resume.txt")


def test_a_file_with_both_words_beats_a_file_with_one(stack) -> None:
    (stack.library / "only-roof.txt").write_text(
        "The roof was replaced in 2019 and the shingles are brown.",
        encoding="utf-8",
    )
    stack.rescan()
    hits = stack.client.post(
        "/graphql",
        json={"query": '{ search(q: "roof leak") { hits { name } } }'},
    ).json()["data"]["search"]["hits"]
    names = [hit["name"] for hit in hits]
    assert names[0] == "roof.txt"
    assert "only-roof.txt" in names
    assert names.index("roof.txt") < names.index("only-roof.txt")


def test_folder_limits_the_hits(stack) -> None:
    archive = stack.library / "archive"
    archive.mkdir()
    (archive / "old.txt").write_text("the roof leak was in the archive", encoding="utf-8")
    stack.rescan()
    hits = stack.client.post(
        "/graphql",
        json={
            "query": 'query ($folder: String) { search(q: "roof leak", folder: $folder) { hits { name } } }',
            "variables": {"folder": "archive"},
        },
    ).json()["data"]["search"]["hits"]
    assert [hit["name"] for hit in hits] == ["old.txt"]


def test_snippet_centers_on_the_words_that_matched() -> None:
    text = ("intro about the house " * 40) + "anup shah published the method" + (" and then more notes" * 40)
    snippet = snippet_for(text, "research paper made by anup shah")
    assert "anup shah" in snippet

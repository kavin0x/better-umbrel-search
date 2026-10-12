from PIL import Image


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

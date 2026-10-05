from httpx import AsyncClient

NOTE = (
    "Coral reefs cover less than one percent of the ocean floor "
    "but shelter a quarter of marine species."
)


async def _collection(client: AsyncClient) -> str:
    return (await client.post("/api/collections", json={})).json()["id"]


async def test_a_note_is_added_named_read_and_removed(client: AsyncClient) -> None:
    cid = await _collection(client)
    r = await client.post(f"/api/collections/{cid}/sources", json={"kind": "text", "text": NOTE})
    assert r.status_code == 201, r.text
    (added,) = r.json()
    assert added["ok"] and added["error"] == ""
    src = added["source"]
    # Titled by its first words when given no title.
    assert src["title"] == "Coral reefs cover less than one"
    assert src["kind"] == "text" and src["chars"] == len(NOTE) and src["name"].endswith(".md")

    # A second note with the same title gets its own name.
    again = await client.post(
        f"/api/collections/{cid}/sources", json={"kind": "text", "text": NOTE}
    )
    assert again.json()[0]["source"]["name"] != src["name"]

    listed = (await client.get(f"/api/collections/{cid}/sources")).json()
    assert listed[0]["name"] == src["name"] and len(listed) == 2
    summary = (await client.get(f"/api/collections/{cid}")).json()["collection"]
    assert summary["sources"] == 2

    read = (await client.get(f"/api/collections/{cid}/sources/{src['name']}")).json()
    assert read["text"] == NOTE

    assert (await client.delete(f"/api/collections/{cid}/sources/{src['name']}")).status_code == 204
    r = await client.delete(f"/api/collections/{cid}/sources/{src['name']}")
    assert r.status_code == 404 and r.json()["detail"].startswith("That source is no longer there")


async def test_a_short_note_is_a_source_and_an_empty_one_is_not(client: AsyncClient) -> None:
    cid = await _collection(client)
    r = await client.post(f"/api/collections/{cid}/sources", json={"kind": "text", "text": "Hi."})
    assert r.status_code == 201 and r.json()[0]["source"]["title"] == "Hi."
    r = await client.post(f"/api/collections/{cid}/sources", json={"kind": "text", "text": " "})
    assert r.status_code == 422
    assert r.json()["detail"] == "The note is empty. Write or paste something, then add it."


async def test_a_titled_note_keeps_its_title_as_a_heading(client: AsyncClient) -> None:
    cid = await _collection(client)
    r = await client.post(
        f"/api/collections/{cid}/sources", json={"kind": "text", "text": NOTE, "title": "Reefs"}
    )
    src = r.json()[0]["source"]
    assert src["title"] == "Reefs" and src["chars"] == len(NOTE) + len("# Reefs\n\n")
    read = (await client.get(f"/api/collections/{cid}/sources/{src['name']}")).json()
    assert read["text"] == f"# Reefs\n\n{NOTE}"


def test_a_source_is_described_by_where_it_came_from_and_its_length() -> None:
    from opennotebook.domain.sources import described

    assert described("p.md", "https://www.example.org/a/b", 1200) == (
        200,
        "example.org · 200 words",
        "https://www.example.org/favicon.ico",
    )
    assert described("report.pdf", "", 600) == (100, "PDF · 100 words", "")
    assert described("note.md", "", 60) == (10, "note · 10 words", "")

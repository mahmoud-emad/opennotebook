from httpx import AsyncClient

from tests.conftest import other_person


async def test_a_collection_is_made_listed_renamed_pinned_and_deleted(client: AsyncClient) -> None:
    r = await client.post("/api/collections", json={"title": "  Coral   reefs "})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["title"] == "Coral reefs" and not c["title_auto"] and c["sources"] == 0

    untitled = (await client.post("/api/collections", json={})).json()
    assert untitled["title"] == "" and untitled["title_auto"]

    listed = (await client.get("/api/collections")).json()
    # Most recently updated first.
    assert [x["id"] for x in listed] == [untitled["id"], c["id"]]

    r = await client.patch(f"/api/collections/{c['id']}", json={"title": "Reefs", "pinned": True})
    assert r.json()["title"] == "Reefs" and r.json()["pinned"]
    # An empty title hands the naming back to the studio.
    r = await client.patch(f"/api/collections/{c['id']}", json={"title": " "})
    assert r.json()["title_auto"]

    detail = (await client.get(f"/api/collections/{c['id']}")).json()
    assert detail["collection"]["id"] == c["id"] and detail["outputs"] == []

    assert (await client.delete(f"/api/collections/{c['id']}")).status_code == 204
    r = await client.get(f"/api/collections/{c['id']}")
    assert r.status_code == 404
    assert r.json() == {
        "detail": "That collection is no longer there. Reload the page to see what is."
    }


async def test_a_sixth_empty_collection_is_refused_in_words(client: AsyncClient) -> None:
    ids = [(await client.post("/api/collections", json={})).json()["id"] for _ in range(5)]
    r = await client.post("/api/collections", json={})
    assert r.status_code == 409
    assert r.json()["detail"].startswith("You already have 5 empty collections.")

    # A collection with a source is not empty, so one more may start.
    note = {"kind": "text", "text": "Reefs are built by colonies of coral polyps over centuries."}
    assert (await client.post(f"/api/collections/{ids[0]}/sources", json=note)).status_code == 201
    assert (await client.post("/api/collections", json={})).status_code == 201


async def test_the_limit_counts_per_person(client: AsyncClient) -> None:
    for _ in range(5):
        await client.post("/api/collections", json={})
    them = await other_person(client, "them@example.com")
    assert (await client.post("/api/collections", json={}, headers=them)).status_code == 201


async def test_one_person_cannot_reach_anothers_collection(client: AsyncClient) -> None:
    mine = (await client.post("/api/collections", json={"title": "Mine"})).json()
    them = await other_person(client, "them@example.com")

    assert (await client.get("/api/collections", headers=them)).json() == []
    for method, path, body in [
        ("GET", f"/api/collections/{mine['id']}", None),
        ("PATCH", f"/api/collections/{mine['id']}", {"title": "Theirs"}),
        ("DELETE", f"/api/collections/{mine['id']}", None),
        ("GET", f"/api/collections/{mine['id']}/sources", None),
        ("GET", f"/api/collections/{mine['id']}/chat", None),
        ("GET", f"/api/collections/{mine['id']}/mindmaps", None),
        ("GET", f"/api/collections/{mine['id']}/notes", None),
        ("POST", f"/api/collections/{mine['id']}/sources", {"kind": "text", "text": "x" * 80}),
    ]:
        r = await client.request(method, path, json=body, headers=them)
        assert r.status_code == 404, (method, path, r.text)
    assert (await client.get(f"/api/collections/{mine['id']}")).json()["collection"][
        "title"
    ] == "Mine"


async def test_a_bad_request_is_answered_in_a_sentence(client: AsyncClient) -> None:
    r = await client.post("/api/collections", json={"title": "x" * 300})
    assert r.status_code == 422
    assert r.json()["detail"] == "Title: string should have at most 200 characters."
    r = await client.get("/api/collections/not-a-uuid")
    assert r.status_code == 422 and r.json()["detail"].startswith("Cid:")
    r = await client.get("/api/nowhere")
    assert r.json()["detail"] == "That is no longer there. Reload the page and try again."

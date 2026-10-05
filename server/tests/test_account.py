import os

from httpx import ASGITransport, AsyncClient

from opennotebook.config import settings
from opennotebook.main import create_app


async def test_health_and_the_local_owner(client: AsyncClient) -> None:
    assert (await client.get("/api/health")).json() == {"ok": True}
    me = (await client.get("/api/me")).json()
    assert me["email"] == "owner@localhost"
    # The same owner on every request.
    assert (await client.get("/api/me")).json()["id"] == me["id"]


async def test_a_key_acts_as_its_owner_until_revoked(client: AsyncClient) -> None:
    owner = (await client.get("/api/me")).json()["id"]
    made = (await client.post("/api/keys", json={"name": "my agent"})).json()
    assert made["key"].startswith("onk_") and made["prefix"] == made["key"][:12]

    auth = {"Authorization": f"Bearer {made['key']}"}
    assert (await client.get("/api/me", headers=auth)).json()["id"] == owner
    listed = (await client.get("/api/keys")).json()
    assert [k["id"] for k in listed] == [made["id"]] and "key" not in listed[0]
    assert listed[0]["last_used_at"] is not None

    assert (await client.delete(f"/api/keys/{made['id']}")).status_code == 204
    r = await client.get("/api/me", headers=auth)
    assert r.status_code == 401
    assert r.json()["detail"].startswith("That API key is not valid or was revoked.")


async def test_keys_mode_asks_for_a_key() -> None:
    os.environ["OPENNOTEBOOK_AUTH"] = "keys"
    settings.cache_clear()
    try:
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/me")
        assert r.status_code == 401 and r.json()["detail"].startswith("Sign in with an API key")
    finally:
        os.environ["OPENNOTEBOOK_AUTH"] = "local"
        settings.cache_clear()

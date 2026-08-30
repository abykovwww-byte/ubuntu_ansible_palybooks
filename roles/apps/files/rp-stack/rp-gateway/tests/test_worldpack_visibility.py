from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_gateway import client, login, write_worldpack




def test_admin_controls_worldpack_visibility_and_private_worlds_are_admin_only(tmp_path: Path):
    write_worldpack(tmp_path, supported_modes=["rp"])
    admin = client(
        tmp_path,
        auth_enabled=True,
        bootstrap_admin_username="admin",
        bootstrap_admin_password="admin-secret",
    )
    login(admin)
    created = admin.post(
        "/api/admin/users",
        json={"username": "player", "password": "player-secret", "role": "user"},
    )
    assert created.status_code == 200, created.text

    player = TestClient(admin.app)
    login(player, "player", "player-secret")
    assert admin.get("/api/worldpacks").json()["worldpacks"][0]["visibility"] == "public"
    assert player.get("/api/worldpacks/demo-world").status_code == 200

    private = admin.patch(
        "/api/admin/worldpacks/demo-world/visibility",
        json={"visibility": "private"},
    )
    assert private.status_code == 200, private.text
    assert private.json()["worldpack"]["visibility"] == "private"
    assert {pack["id"] for pack in admin.get("/api/worldpacks").json()["worldpacks"]} == {"demo-world"}
    assert player.get("/api/worldpacks").json()["worldpacks"] == []
    assert player.get("/api/worldpacks/demo-world").status_code == 404
    assert player.get("/api/worldpacks/demo-world/player-templates").status_code == 404
    assert player.post(
        "/api/player-characters",
        json={"worldpack_id": "demo-world", "name": "Blocked", "description": "Blocked", "profile": {}},
    ).status_code == 400
    assert player.patch(
        "/api/admin/worldpacks/demo-world/visibility",
        json={"visibility": "public"},
    ).status_code == 403

    public = admin.patch(
        "/api/admin/worldpacks/demo-world/visibility",
        json={"visibility": "public"},
    )
    assert public.status_code == 200, public.text
    assert player.get("/api/worldpacks/demo-world").status_code == 200

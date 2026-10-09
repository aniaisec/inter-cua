"""Inventory state is an independent oracle, not a replay output assertion."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from cua.cli import main
from cua.initialize import initialize
from examples.inventory.app.app import create_app


def test_stock_target_validates_and_consumes_each_pending_write_once():
    with TestClient(create_app()) as client:
        assert client.get("/items/SKU-001").url.path == "/login"
        assert (
            "Invalid inventory credentials" in client.post("/login", data={"username": "bad"}).text
        )
        client.post("/login", data={"username": "clerk", "password": "practice-password"})
        assert "Item not found" in client.post("/lookup", data={"item_code": "SKU-999"}).text
        for value, message in (
            ("-100", "Insufficient stock"),
            ("0", "nonzero integer"),
            ("oops", "nonzero integer"),
        ):
            assert message in client.post("/adjust/SKU-001", data={"quantity_change": value}).text
        before = client.get("/_test/state").json()
        assert before["quantities"] == {"SKU-001": 12, "SKU-002": 4}
        assert before["commit_count"] == 0
        client.post("/adjust/SKU-001", data={"quantity_change": "-2"})
        assert "Adjustment saved" in client.post("/review/SKU-001").text
        assert client.post("/review/SKU-001").status_code == 409
        after = client.get("/_test/state").json()
        assert after["quantities"]["SKU-001"] == 10
        assert after["commit_count"] == 1 and after["commit_posts"] == 2
        assert after["adjustments"] == [
            {"receipt": "ADJ-0001", "item_code": "SKU-001", "quantity_change": -2, "quantity": 10}
        ]


def test_stock_changed_by_another_session_is_rechecked_at_commit():
    app = create_app()
    with TestClient(app) as first, TestClient(app) as second:
        for client in (first, second):
            client.post("/login", data={"username": "clerk", "password": "practice-password"})
            client.post("/adjust/SKU-002", data={"quantity_change": "-3"})
        assert "Adjustment saved" in first.post("/review/SKU-002").text
        assert "Insufficient stock" in second.post("/review/SKU-002").text
        state = second.get("/_test/state").json()
        assert state["quantities"]["SKU-002"] == 1
        assert state["commit_count"] == 1


def test_missing_inventory_family_fails_before_provider_or_browser(tmp_path, capsys):
    initialize(tmp_path, "inventory")
    (tmp_path / ".env").write_bytes((tmp_path / ".env.example").read_bytes())
    (tmp_path / "capabilities/families/stockroom.yaml").unlink()
    with (
        patch("cua.agent.llm.select_client", side_effect=AssertionError("model work")),
        patch("cua.agent.llm.ScriptedClient", side_effect=AssertionError("scripted model work")),
        patch(
            "cua.surface.playwright_surface.PlaywrightSurface.launch",
            side_effect=AssertionError("UI launch"),
        ),
    ):
        assert (
            main(
                [
                    "--root",
                    str(tmp_path),
                    "discover",
                    "--name",
                    "item_lookup",
                    "--goal",
                    "Look up an item",
                    "--llm",
                    "scripted",
                    "--script",
                    str(tmp_path / "scripts/discovery/item_lookup.yaml"),
                ]
            )
            == 64
        )
    assert "stockroom" in capsys.readouterr().err
    assert not (tmp_path / "evidence").exists()

"""The paired television reads the same Home model as the web.

One projection, one layout store, one banner choice. These tests pin the native
endpoints so the TV cannot drift into a private copy of Home, and they check that
Edit Home from the remote persists for that profile.

The `system` fixture is imported from the module that defines it so pytest can
resolve it here; Ruff sees that as an unused redefinition, hence the file-level noqa.
"""

# ruff: noqa: F401, F811

from tests.plus.test_native_tv_test import LIBRARY_API, _anime_pass, system


def _home(native):
    response = native.get(LIBRARY_API + "/home")
    assert response.status_code == 200, response.text
    return response.json()


def test_home_projection_carries_the_shared_rows_and_banner(system, monkeypatch):
    native, *_ = _anime_pass(system, monkeypatch)
    home = _home(native)
    assert {"banner", "rows", "layout", "addable", "hidden", "empty"} <= set(home)
    ids = [row["id"] for row in home["rows"]]
    # The three agreed defaults come first, and the separate New Episodes row is gone.
    assert ids[:3] == ["continue-anime", "continue-movies", "continue-tv"]
    assert "new-episodes" not in ids
    assert "ready-to-watch" not in ids
    banner = home["banner"]
    assert {"title", "artwork"} <= set(banner)
    for row in home["rows"]:
        assert {"id", "label", "kind", "domain", "count", "cards"} <= set(row)


def test_edit_home_from_the_remote_persists(system, monkeypatch):
    native, *_ = _anime_pass(system, monkeypatch)
    first = _home(native)["layout"]["rows"][0]["id"]

    moved = native.post(LIBRARY_API + "/home/layout", json={"action": "move-down", "target": first})
    assert moved.status_code == 200, moved.text
    assert _home(native)["layout"]["rows"][1]["id"] == first

    hidden = native.post(LIBRARY_API + "/home/layout", json={"action": "remove", "target": first})
    assert hidden.status_code == 200
    assert first not in [row["id"] for row in _home(native)["rows"]]

    restored = native.post(LIBRARY_API + "/home/layout", json={"action": "add", "target": first})
    assert restored.status_code == 200
    assert first in [row["id"] for row in _home(native)["rows"]]

    reset = native.post(LIBRARY_API + "/home/layout", json={"action": "reset"})
    assert reset.status_code == 200
    assert _home(native)["layout"]["rows"][0]["id"] == first


def test_edit_home_rejects_an_unknown_action_without_changing_the_layout(system, monkeypatch):
    native, *_ = _anime_pass(system, monkeypatch)
    before = _home(native)["layout"]
    bad = native.post(LIBRARY_API + "/home/layout", json={"action": "explode"})
    assert bad.status_code == 400
    assert _home(native)["layout"] == before


def test_edit_home_refuses_to_hide_every_row(system, monkeypatch):
    native, *_ = _anime_pass(system, monkeypatch)
    rows = [row["id"] for row in _home(native)["layout"]["rows"]]
    for identifier in rows[:-1]:
        assert (
            native.post(
                LIBRARY_API + "/home/layout", json={"action": "remove", "target": identifier}
            ).status_code
            == 200
        )
    last = native.post(LIBRARY_API + "/home/layout", json={"action": "remove", "target": rows[-1]})
    assert last.status_code == 422, "a profile must keep at least one visible shelf"
    assert _home(native)["rows"], "the visible shelf survived the refusal"


def test_edit_home_bulk_rows_round_trip(system, monkeypatch):
    native, *_ = _anime_pass(system, monkeypatch)
    initial = _home(native)["layout"]["rows"]
    reordered = [{"id": row["id"], "hidden": i == 0} for i, row in enumerate(reversed(initial))]
    saved = native.post(LIBRARY_API + "/home/layout", json={"rows": reordered})
    assert saved.status_code == 200, saved.text
    after = _home(native)["layout"]["rows"]
    assert [(row["id"], row["hidden"]) for row in after[: len(reordered)]] == [
        (entry["id"], entry["hidden"]) for entry in reordered
    ]
    assert after[0]["id"] not in [row["id"] for row in _home(native)["rows"]]
    # Round-tripping the full returned layout (including catalog defaults appended as hidden)
    # preserves exact length and order.
    second_order = [{"id": row["id"], "hidden": row["hidden"]} for row in reversed(after)]
    assert native.post(LIBRARY_API + "/home/layout", json={"rows": second_order}).status_code == 200
    after_second = _home(native)["layout"]["rows"]
    assert [(row["id"], row["hidden"]) for row in after_second] == [
        (entry["id"], entry["hidden"]) for entry in second_order
    ]
    all_hidden = native.post(
        LIBRARY_API + "/home/layout",
        json={"rows": [{"id": row["id"], "hidden": True} for row in after]},
    )
    assert all_hidden.status_code == 422
    assert [(row["id"], row["hidden"]) for row in _home(native)["layout"]["rows"]] == [
        (entry["id"], entry["hidden"]) for entry in second_order
    ]

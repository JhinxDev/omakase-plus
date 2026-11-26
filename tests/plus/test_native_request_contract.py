"""The Apple TV request payload carries the shared model, not a summary.

Both clients must show the same stages, the same stopped point and the same
honest unknown reason. These tests pin the projection so a TV change cannot drift
from the web one, and so no browser link or provider metadata leaks into the
native contract.
"""

from omakase.plus import native_tv_test


def _model(**overrides):
    model = {
        "identity": "movie:tt0328955",
        "kind": "movie",
        "media_kind": "movie",
        "title_id": "tt0328955",
        "title": "Chouchou",
        "season": None,
        "supported_stages": ["saved", "received", "searched", "acquired", "library"],
        "stage": "acquired",
        "stage_label": "Acquiring",
        "stages": [
            {"stage": "saved", "label": "Saved", "state": "done", "occurred_at": 100},
            {"stage": "received", "label": "Received", "state": "done", "occurred_at": 110},
            {"stage": "searched", "label": "Searched", "state": "done", "occurred_at": 120},
            {
                "stage": "acquired",
                "label": "Acquiring",
                "state": "stopped",
                "occurred_at": 130,
            },
            {"stage": "library", "label": "In library", "state": "unknown", "occurred_at": None},
        ],
        "state": "failed",
        "label": "Acquisition failed",
        "status_label": "Acquisition failed",
        "detail": "Two candidates were rejected.",
        "category": "attention",
        "manual": False,
        "manual_note": None,
        "stopped_at": "acquired",
        "reason": None,
        "unknown_reason": True,
        "reason_text": "The exact provider reason was not supplied.",
        "last_checked_at": 200,
        "last_confirmed_at": 130,
        "next_action": {"kind": "retry", "label": "Retry search", "enabled": True, "reason": None},
        "can_check": True,
        "can_retry": True,
        "resolved": False,
        "removed": False,
        "candidates_rejected": 2,
        "href": "/plus/requests/movie/tt0328955",
        "detail_href": "/plus/requests/movie/tt0328955",
        "timeline": [],
    }
    model.update(overrides)
    return model


def test_the_native_payload_carries_the_shared_stages_and_reason():
    payload = native_tv_test.native_request_status(_model())
    assert payload["category"] == "attention"
    assert payload["stopped_at"] == "acquired"
    assert payload["unknown_reason"] is True
    assert payload["reason_text"] == "The exact provider reason was not supplied."
    assert payload["candidates_rejected"] == 2
    assert payload["next_action"]["label"] == "Retry search"
    assert payload["can_retry"] is True
    assert payload["last_checked_at"] == 200
    assert [entry["stage"] for entry in payload["stages"]] == [
        "saved",
        "received",
        "searched",
        "acquired",
        "library",
    ]
    assert payload["stages"][3]["state"] == "stopped"
    assert payload["stages"][4]["state"] == "unknown"


def test_web_links_and_provider_metadata_stay_out_of_the_native_contract():
    payload = native_tv_test.native_request_status(
        _model(provider_secret="https://provider.invalid/xyz", internal_note="hidden")
    )
    assert "href" not in payload and "detail_href" not in payload
    assert "timeline" not in payload
    assert "provider_secret" not in payload
    assert "internal_note" not in payload


def test_a_manual_tv_request_says_it_is_manual():
    payload = native_tv_test.native_request_status(
        _model(
            kind="tv",
            media_kind="tv",
            title_id="42",
            season=2,
            supported_stages=["saved", "library"],
            manual=True,
            manual_note="Ask the owner to add this season.",
            next_action={
                "kind": "manual",
                "label": "Request sent",
                "enabled": False,
                "reason": "manual_acquisition",
            },
        )
    )
    assert payload["manual"] is True
    assert payload["manual_note"] == "Ask the owner to add this season."
    assert payload["next_action"]["kind"] == "manual"
    assert payload["supported_stages"] == ["saved", "library"]


def test_malformed_or_missing_rows_do_not_invent_a_state():
    assert native_tv_test.native_request_status(None) is None
    assert native_tv_test.native_request_status({"title": "x"})["stages"] == []
    payload = native_tv_test.native_request_status(
        _model(stages=[{"label": "no stage key"}, "junk", {"stage": "saved"}])
    )
    assert payload["stages"] == [
        {"stage": "saved", "label": "", "state": "unknown", "occurred_at": None}
    ]


def test_only_fetchable_poster_targets_reach_the_native_client():
    tmdb = "https://image.tmdb.org/t/p/w342/abc_123.jpg"
    assert native_tv_test.native_request_status(_model(poster=tmdb))["poster"] == tmdb
    native = "artwork/12/poster"
    assert native_tv_test.native_request_status(_model(poster=native))["poster"] == native
    assert native_tv_test.native_request_status(_model())["poster"] is None
    for hostile in (
        "http://image.tmdb.org/t/p/w342/a.jpg",
        "https://evil.example/t/p/w342/a.jpg",
        "https://image.tmdb.org/t/p/w342/a.jpg?x=1",
        "artwork/0/poster",
        "artwork/1/poster/../../x",
        "/plus/movies/tt1/artwork?variant=poster",
        7,
    ):
        assert native_tv_test.native_request_status(_model(poster=hostile))["poster"] is None

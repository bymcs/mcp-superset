"""Native dashboard filter tools.

They read a dashboard's json_metadata, edit its ``native_filter_configuration`` and PUT the whole
metadata back, so the things worth pinning are: unrelated metadata survives, only the intended
filter changes, and destructive operations refuse to run without confirmation.
(Null-metadata crashes are covered in test_tools_end_to_end.py.)
"""

import json

import httpx
import pytest
import respx
from fastmcp import Client, FastMCP

import mcp_superset.server as server_module
from mcp_superset.auth import CookieAuthManager
from mcp_superset.client import SupersetClient
from mcp_superset.tools import register_all_tools

BASE = "https://superset.example.com"
DASH = f"{BASE}/api/v1/dashboard/31"


def _filter(fid, name, column="region", dataset_id=7, parents=()):
    return {
        "id": fid,
        "name": name,
        "filterType": "filter_select",
        "type": "NATIVE_FILTER",
        "targets": [{"datasetId": dataset_id, "column": {"name": column}}],
        "controlValues": {"multiSelect": True, "searchAllOptions": False, "enableEmptyFilter": False},
        "cascadeParentIds": list(parents),
        "chartsInScope": [65],
    }


F_REGION = _filter("NATIVE_FILTER-aaa", "Region")
F_CITY = _filter("NATIVE_FILTER-bbb", "City", column="city", parents=("NATIVE_FILTER-aaa",))

POSITION = {
    "ROOT_ID": {"type": "ROOT", "children": ["GRID_ID"]},
    "CHART-1": {"type": "CHART", "meta": {"chartId": 65}},
    "CHART-2": {"type": "CHART", "meta": {"chartId": 66}},
}


@pytest.fixture
def mcp_server(monkeypatch):
    client = SupersetClient(auth_manager=CookieAuthManager(base_url=BASE, cookie_value="c"), base_url=BASE)
    monkeypatch.setattr(server_module, "superset_client", client)
    mcp = FastMCP(name="test")
    register_all_tools(mcp)
    return mcp


def _text(result):
    content = getattr(result, "content", result)
    if isinstance(content, list) and content:
        return content[0].text
    return str(content)


def _mock_dashboard(filters=(F_REGION, F_CITY), extra_metadata=None):
    """Mock the dashboard and the follow-up calls the add/reset tools make. Returns the PUT route."""
    metadata = {"color_scheme": "supersetColors", "native_filter_configuration": [dict(f) for f in filters]}
    metadata.update(extra_metadata or {})
    respx.get(f"{BASE}/api/v1/security/csrf_token/").mock(return_value=httpx.Response(200, json={"result": "csrf"}))
    respx.get(DASH).mock(
        return_value=httpx.Response(
            200,
            json={"result": {"id": 31, "json_metadata": json.dumps(metadata), "position_json": json.dumps(POSITION)}},
        )
    )
    respx.get(f"{DASH}/datasets").mock(return_value=httpx.Response(200, json={"result": []}))
    respx.get(f"{DASH}/charts").mock(return_value=httpx.Response(200, json={"result": []}))
    return respx.put(DASH).mock(return_value=httpx.Response(200, json={"result": {}}))


async def _call(mcp_server, tool, args):
    async with Client(mcp_server) as c:
        return json.loads(_text(await c.call_tool(tool, {"dashboard_id": 31, **args})))


def _sent_metadata(put):
    body = json.loads(put.calls.last.request.content)
    assert list(body) == ["json_metadata"]  # nothing else on the dashboard is overwritten
    return json.loads(body["json_metadata"])


# -------------------------------------------------------------------- list


@respx.mock
async def test_list_summarises_each_filter(mcp_server):
    _mock_dashboard()

    out = await _call(mcp_server, "superset_dashboard_filter_list", {})

    assert [f["name"] for f in out] == ["Region", "City"]
    city = out[1]
    assert city["column"] == "city" and city["datasetId"] == 7
    assert city["cascadeParentIds"] == ["NATIVE_FILTER-aaa"]
    assert city["controlValues"]["multiSelect"] is True


# --------------------------------------------------------------------- add


@respx.mock
async def test_add_appends_and_preserves_existing_filters_and_unrelated_metadata(mcp_server):
    put = _mock_dashboard()

    out = await _call(
        mcp_server,
        "superset_dashboard_filter_add",
        {"name": "Segment", "column": "segment", "dataset_id": 9, "cascade_parent_id": "NATIVE_FILTER-aaa"},
    )

    meta = _sent_metadata(put)
    filters = meta["native_filter_configuration"]
    assert [f["name"] for f in filters] == ["Region", "City", "Segment"]
    new = filters[-1]
    assert new["id"] == out["filter_id"] and new["id"].startswith("NATIVE_FILTER-")
    assert new["targets"] == [{"datasetId": 9, "column": {"name": "segment"}}]
    assert new["cascadeParentIds"] == ["NATIVE_FILTER-aaa"]
    assert new["chartsInScope"] == [65, 66]  # every chart found in position_json
    assert out["chartsInScope"] == [65, 66]
    assert meta["color_scheme"] == "supersetColors"  # unrelated metadata survives
    assert meta["show_native_filters"] is True


@respx.mock
async def test_add_applies_control_flags(mcp_server):
    put = _mock_dashboard(filters=())

    await _call(
        mcp_server,
        "superset_dashboard_filter_add",
        {"name": "Q", "column": "q", "dataset_id": 7, "multi_select": False, "search_all_options": True},
    )

    cv = _sent_metadata(put)["native_filter_configuration"][0]["controlValues"]
    assert cv["multiSelect"] is False and cv["searchAllOptions"] is True and cv["enableEmptyFilter"] is False


# ------------------------------------------------------------------ update


@respx.mock
async def test_update_changes_only_the_requested_fields_of_one_filter(mcp_server):
    put = _mock_dashboard()

    out = await _call(
        mcp_server,
        "superset_dashboard_filter_update",
        {"filter_id": "NATIVE_FILTER-bbb", "name": "Town", "column": "town", "multi_select": False},
    )

    filters = _sent_metadata(put)["native_filter_configuration"]
    assert filters[0] == F_REGION  # the other filter is byte-for-byte unchanged
    town = filters[1]
    assert town["name"] == "Town" and town["targets"][0]["column"]["name"] == "town"
    assert town["controlValues"]["multiSelect"] is False
    assert town["controlValues"]["searchAllOptions"] is False  # untouched
    assert town["cascadeParentIds"] == ["NATIVE_FILTER-aaa"]  # no cascade argument -> unchanged
    assert out == {"status": "ok", "filter_id": "NATIVE_FILTER-bbb"}


@respx.mock
async def test_update_with_empty_cascade_parent_removes_the_cascade(mcp_server):
    put = _mock_dashboard()

    await _call(
        mcp_server, "superset_dashboard_filter_update", {"filter_id": "NATIVE_FILTER-bbb", "cascade_parent_id": ""}
    )

    assert _sent_metadata(put)["native_filter_configuration"][1]["cascadeParentIds"] == []


@respx.mock
async def test_update_unknown_filter_reports_not_found_and_writes_nothing(mcp_server):
    put = _mock_dashboard()

    out = await _call(mcp_server, "superset_dashboard_filter_update", {"filter_id": "NATIVE_FILTER-nope", "name": "x"})

    assert out["status"] == "error" and "not found" in out["message"]
    assert not put.called


# ------------------------------------------------------------------ delete


@respx.mock
async def test_delete_is_rejected_without_confirmation_and_writes_nothing(mcp_server):
    put = _mock_dashboard()

    out = await _call(mcp_server, "superset_dashboard_filter_delete", {"filter_id": "NATIVE_FILTER-aaa"})

    assert "REJECTED" in out["error"] and "Region" in out["error"] and "Total filters on dashboard: 2" in out["error"]
    assert not put.called


@respx.mock
async def test_delete_removes_only_the_named_filter(mcp_server):
    put = _mock_dashboard()

    out = await _call(
        mcp_server, "superset_dashboard_filter_delete", {"filter_id": "NATIVE_FILTER-aaa", "confirm_delete": True}
    )

    meta = _sent_metadata(put)
    assert [f["id"] for f in meta["native_filter_configuration"]] == ["NATIVE_FILTER-bbb"]
    assert meta["color_scheme"] == "supersetColors"
    assert out == {"status": "ok", "deleted": "NATIVE_FILTER-aaa", "remaining": 1}


@respx.mock
async def test_delete_unknown_filter_reports_not_found_even_when_confirmed(mcp_server):
    put = _mock_dashboard()

    out = await _call(
        mcp_server, "superset_dashboard_filter_delete", {"filter_id": "NATIVE_FILTER-nope", "confirm_delete": True}
    )

    assert out["status"] == "error"
    assert not put.called


# ------------------------------------------------------------------- reset

RESET_DEFS = json.dumps(
    [
        {"name": "Region", "column": "region"},
        {"name": "City", "column": "city", "multi_select": False, "cascade_parent_id": "NATIVE_FILTER-keep"},
    ]
)


@respx.mock
async def test_reset_is_rejected_without_confirmation_and_names_what_would_be_lost(mcp_server):
    put = _mock_dashboard()

    out = await _call(mcp_server, "superset_dashboard_filter_reset", {"dataset_id": 7, "filters_json": RESET_DEFS})

    assert "REJECTED" in out["error"] and "Region" in out["error"] and "City" in out["error"]
    assert not put.called


@respx.mock
async def test_reset_replaces_every_filter_and_keeps_unrelated_metadata(mcp_server):
    put = _mock_dashboard(extra_metadata={"refresh_frequency": 60})

    out = await _call(
        mcp_server,
        "superset_dashboard_filter_reset",
        {"dataset_id": 7, "filters_json": RESET_DEFS, "confirm_reset": True},
    )

    meta = _sent_metadata(put)
    filters = meta["native_filter_configuration"]
    old_ids = {"NATIVE_FILTER-aaa", "NATIVE_FILTER-bbb"}
    assert [f["name"] for f in filters] == ["Region", "City"]
    assert not old_ids & {f["id"] for f in filters}  # ids are regenerated
    assert out["filters_created"] == 2 and out["filter_ids"] == [f["id"] for f in filters]
    assert filters[1]["controlValues"]["multiSelect"] is False
    assert filters[1]["cascadeParentIds"] == ["NATIVE_FILTER-keep"]
    assert all(f["chartsInScope"] == [65, 66] for f in filters)
    assert meta["filter_scopes"] == {} and meta["show_native_filters"] is True
    assert meta["color_scheme"] == "supersetColors" and meta["refresh_frequency"] == 60


@respx.mock
async def test_reset_with_invalid_json_changes_nothing(mcp_server):
    put = _mock_dashboard()

    out = await _call(
        mcp_server,
        "superset_dashboard_filter_reset",
        {"dataset_id": 7, "filters_json": "{not json", "confirm_reset": True},
    )

    assert "error" in out
    assert not put.called  # the existing filters must not be wiped by a typo

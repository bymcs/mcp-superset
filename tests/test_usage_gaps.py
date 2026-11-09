"""Gaps found while using the server against a real Superset 6.1.0.

1. dataset_update preserved always_filter_main_dttm only when columns were replaced; a
   sql-only (or metrics/description-only) update let Superset reset it to false.
2. chart_update/chart_create demanded granularity_sqla even for a dataset with no temporal
   column at all, so such charts could never be updated through the server.
3. No tool carried readOnlyHint/destructiveHint annotations.
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
from mcp_superset.tools.annotations import annotations_for

BASE = "https://superset.example.com"
TABLE_PARAMS = json.dumps({"viz_type": "table", "query_mode": "raw", "all_columns": ["id"]})


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


def _mock_csrf():
    respx.get(f"{BASE}/api/v1/security/csrf_token/").mock(return_value=httpx.Response(200, json={"result": "csrf"}))


def _mock_dataset(dataset_id, **fields):
    body = {"id": dataset_id, "main_dttm_col": None, "columns": [], **fields}
    respx.get(f"{BASE}/api/v1/dataset/{dataset_id}").mock(return_value=httpx.Response(200, json={"result": body}))


# --- 1. dataset_update keeps always_filter_main_dttm on every update -----------------------


@respx.mock
@pytest.mark.parametrize("field", [{"sql": "SELECT 1 AS id"}, {"description": "d"}, {"table_name": "t2"}])
async def test_dataset_update_preserves_flag_when_not_replacing_columns(mcp_server, field):
    _mock_csrf()
    _mock_dataset(7, always_filter_main_dttm=True)
    put = respx.put(f"{BASE}/api/v1/dataset/7").mock(return_value=httpx.Response(200, json={"id": 7, "result": {}}))

    async with Client(mcp_server) as c:
        await c.call_tool("superset_dataset_update", {"dataset_id": 7, **field})

    sent = json.loads(put.calls.last.request.content)
    assert sent["always_filter_main_dttm"] is True


@respx.mock
async def test_dataset_update_explicit_flag_wins_and_skips_the_lookup(mcp_server):
    _mock_csrf()
    get = respx.get(f"{BASE}/api/v1/dataset/7").mock(
        return_value=httpx.Response(200, json={"result": {"always_filter_main_dttm": True}})
    )
    put = respx.put(f"{BASE}/api/v1/dataset/7").mock(return_value=httpx.Response(200, json={"id": 7, "result": {}}))

    async with Client(mcp_server) as c:
        await c.call_tool("superset_dataset_update", {"dataset_id": 7, "always_filter_main_dttm": False})

    assert json.loads(put.calls.last.request.content)["always_filter_main_dttm"] is False
    assert get.call_count == 0


# --- 2. granularity_sqla is not demanded where it cannot apply -----------------------------


@respx.mock
async def test_chart_update_allowed_when_dataset_has_no_temporal_column(mcp_server):
    _mock_csrf()
    respx.get(f"{BASE}/api/v1/chart/29").mock(
        return_value=httpx.Response(200, json={"result": {"id": 29, "datasource_id": 7}})
    )
    _mock_dataset(7, columns=[{"column_name": "id", "is_dttm": False}])
    put = respx.put(f"{BASE}/api/v1/chart/29").mock(return_value=httpx.Response(200, json={"id": 29, "result": {}}))

    async with Client(mcp_server) as c:
        result = await c.call_tool(
            "superset_chart_update", {"chart_id": 29, "params": TABLE_PARAMS, "confirm_params_replace": True}
        )

    assert "error" not in json.loads(_text(result))
    assert put.called


@respx.mock
@pytest.mark.parametrize(
    "dataset",
    [
        {"main_dttm_col": "created_at", "columns": []},
        {"columns": [{"column_name": "ts", "is_dttm": True}]},
    ],
)
async def test_chart_update_still_requires_granularity_when_dataset_has_a_temporal_column(mcp_server, dataset):
    _mock_csrf()
    respx.get(f"{BASE}/api/v1/chart/29").mock(
        return_value=httpx.Response(200, json={"result": {"id": 29, "datasource_id": 7}})
    )
    _mock_dataset(7, **dataset)
    put = respx.put(f"{BASE}/api/v1/chart/29").mock(return_value=httpx.Response(200, json={"id": 29, "result": {}}))

    async with Client(mcp_server) as c:
        result = await c.call_tool(
            "superset_chart_update", {"chart_id": 29, "params": TABLE_PARAMS, "confirm_params_replace": True}
        )

    assert "granularity_sqla" in json.loads(_text(result))["error"]
    assert not put.called


@respx.mock
async def test_chart_update_stays_conservative_when_dataset_cannot_be_read(mcp_server):
    _mock_csrf()
    respx.get(f"{BASE}/api/v1/chart/29").mock(
        return_value=httpx.Response(200, json={"result": {"id": 29, "datasource_id": 7}})
    )
    respx.get(f"{BASE}/api/v1/dataset/7").mock(return_value=httpx.Response(404, json={"message": "nf"}))
    put = respx.put(f"{BASE}/api/v1/chart/29").mock(return_value=httpx.Response(200, json={"id": 29, "result": {}}))

    async with Client(mcp_server) as c:
        result = await c.call_tool(
            "superset_chart_update", {"chart_id": 29, "params": TABLE_PARAMS, "confirm_params_replace": True}
        )

    assert "granularity_sqla" in json.loads(_text(result))["error"]
    assert not put.called


@respx.mock
async def test_chart_update_with_granularity_never_needs_a_dataset_lookup(mcp_server):
    _mock_csrf()
    chart_get = respx.get(f"{BASE}/api/v1/chart/29").mock(return_value=httpx.Response(200, json={"result": {}}))
    put = respx.put(f"{BASE}/api/v1/chart/29").mock(return_value=httpx.Response(200, json={"id": 29, "result": {}}))
    params = json.dumps({"viz_type": "table", "granularity_sqla": "created_at"})

    async with Client(mcp_server) as c:
        await c.call_tool("superset_chart_update", {"chart_id": 29, "params": params, "confirm_params_replace": True})

    assert put.called
    assert chart_get.call_count == 0


@respx.mock
async def test_chart_create_allowed_when_dataset_has_no_temporal_column(mcp_server):
    _mock_csrf()
    _mock_dataset(7, columns=[{"column_name": "id", "is_dttm": False}])
    post = respx.post(f"{BASE}/api/v1/chart/").mock(return_value=httpx.Response(201, json={"id": 30, "result": {}}))

    async with Client(mcp_server) as c:
        result = await c.call_tool(
            "superset_chart_create",
            {"slice_name": "t", "viz_type": "table", "datasource_id": 7, "params": TABLE_PARAMS},
        )

    assert "error" not in json.loads(_text(result))
    assert post.called


# --- 3. annotations -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("superset_chart_list", "read"),
        ("superset_dataset_get", "read"),
        ("superset_get_base_url", "read"),
        ("superset_chart_get_data", "read"),
        ("superset_chart_data", "read"),
        ("superset_dashboard_export", "read"),
        ("superset_database_validate_sql", "read"),
        ("superset_chart_update", "write"),
        ("superset_dataset_create", "write"),
        ("superset_sqllab_execute", "write"),
        ("superset_group_add_users", "write"),
        ("superset_chart_delete", "destructive"),
        ("superset_user_delete", "destructive"),
        ("superset_group_remove_users", "destructive"),
        ("superset_dashboard_filter_reset", "destructive"),
        ("superset_bulk_user_role_replace", "destructive"),
        ("superset_role_permission_add", "destructive"),
        ("superset_assets_import", "destructive"),
        ("superset_query_stop", "destructive"),
    ],
)
def test_annotation_classification(name, expected):
    ann = annotations_for(name)
    kind = "read" if ann["readOnlyHint"] else ("destructive" if ann["destructiveHint"] else "write")
    assert kind == expected


def _hint(annotations, camel):
    """Read an MCP annotation across SDK versions (fastmcp 3.x: readOnlyHint, 4.x: read_only_hint)."""
    snake = "".join("_" + ch.lower() if ch.isupper() else ch for ch in camel)
    return getattr(annotations, snake) if hasattr(annotations, snake) else getattr(annotations, camel, None)


async def test_every_registered_tool_carries_annotations(mcp_server):
    async with Client(mcp_server) as c:
        tools = await c.list_tools()

    assert len(tools) >= 130
    missing = [t.name for t in tools if t.annotations is None or _hint(t.annotations, "readOnlyHint") is None]
    assert not missing, missing
    by_name = {t.name: t.annotations for t in tools}
    assert _hint(by_name["superset_chart_list"], "readOnlyHint") is True
    assert _hint(by_name["superset_user_delete"], "destructiveHint") is True
    assert _hint(by_name["superset_chart_update"], "readOnlyHint") is False
    assert _hint(by_name["superset_chart_update"], "destructiveHint") is False


def test_tool_decorator_is_restored_after_registration(mcp_server):
    assert "tool" not in vars(mcp_server)

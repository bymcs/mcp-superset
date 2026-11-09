"""superset_dataset_column_add: add or update ONE dataset column without touching the rest.

Superset's PUT /dataset/{id} replaces ALL columns with whatever list it receives, so changing
one column through superset_dataset_update means rebuilding the whole list by hand. This tool
does that round trip: read the dataset, change one column, write every column back with its id.
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

# What GET /api/v1/dataset/{id} returns for a column: writable fields plus read-only ones.
COL_ID = {
    "id": 11,
    "uuid": "u-11",
    "changed_on": "2026-01-01T00:00:00",
    "column_name": "ID",
    "verbose_name": "Sicil",
    "description": None,
    "expression": None,
    "filterable": True,
    "groupby": True,
    "is_active": True,
    "is_dttm": False,
    "python_date_format": None,
    "type": "INTEGER",
    "type_generic": 0,
    "advanced_data_type": None,
    "extra": '{"warning_markdown":null}',
}
COL_NAME = {
    **COL_ID,
    "id": 12,
    "uuid": "u-12",
    "column_name": "NAME",
    "verbose_name": None,
    "type": "STRING",
    "type_generic": 1,
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


def _mock_dataset(columns=None, flag=True):
    respx.get(f"{BASE}/api/v1/security/csrf_token/").mock(return_value=httpx.Response(200, json={"result": "csrf"}))
    body = {"id": 7, "always_filter_main_dttm": flag, "columns": [COL_ID, COL_NAME] if columns is None else columns}
    respx.get(f"{BASE}/api/v1/dataset/7").mock(return_value=httpx.Response(200, json={"result": body}))
    return respx.put(f"{BASE}/api/v1/dataset/7").mock(return_value=httpx.Response(200, json={"id": 7, "result": {}}))


async def _call(mcp_server, args):
    async with Client(mcp_server) as c:
        return json.loads(_text(await c.call_tool("superset_dataset_column_add", {"dataset_id": 7, **args})))


@respx.mock
async def test_adds_a_new_column_and_keeps_every_existing_one(mcp_server):
    put = _mock_dataset()

    out = await _call(mcp_server, {"column_name": "YAKA_TIPI", "verbose_name": "Yaka Tipi"})

    sent = json.loads(put.calls.last.request.content)
    assert [c["column_name"] for c in sent["columns"]] == ["ID", "NAME", "YAKA_TIPI"]
    assert sent["columns"][0]["id"] == 11 and sent["columns"][1]["id"] == 12  # existing columns keep their ids
    assert "id" not in sent["columns"][2]  # the new column gets none
    assert sent["columns"][2]["verbose_name"] == "Yaka Tipi"
    assert sent["columns"][0]["verbose_name"] == "Sicil"  # existing metadata survives the round trip
    assert out["action"] == "added" and out["columns_total"] == 3


@respx.mock
async def test_existing_columns_are_written_back_without_read_only_fields_or_nulls(mcp_server):
    put = _mock_dataset()

    await _call(mcp_server, {"column_name": "YAKA_TIPI"})

    first = json.loads(put.calls.last.request.content)["columns"][0]
    for read_only in ("uuid", "changed_on", "type_generic"):
        assert read_only not in first
    assert None not in first.values()  # Superset rejects explicit nulls on some column fields
    assert first["type"] == "INTEGER" and first["extra"] == '{"warning_markdown":null}'


@respx.mock
async def test_always_filter_main_dttm_is_preserved(mcp_server):
    put = _mock_dataset(flag=True)
    await _call(mcp_server, {"column_name": "YAKA_TIPI"})
    assert json.loads(put.calls.last.request.content)["always_filter_main_dttm"] is True


@respx.mock
async def test_updating_an_existing_column_changes_only_that_column(mcp_server):
    put = _mock_dataset()

    out = await _call(mcp_server, {"column_name": "NAME", "verbose_name": "Ad", "description": "kişi adı"})

    cols = json.loads(put.calls.last.request.content)["columns"]
    assert [c["column_name"] for c in cols] == ["ID", "NAME"]  # no duplicate
    assert cols[1]["id"] == 12 and cols[1]["verbose_name"] == "Ad" and cols[1]["description"] == "kişi adı"
    assert cols[0]["verbose_name"] == "Sicil"
    assert out["action"] == "updated"


@respx.mock
async def test_calculated_column_expression_is_sent(mcp_server):
    put = _mock_dataset()

    await _call(
        mcp_server, {"column_name": "YAKA", "expression": "CASE WHEN ID = 1 THEN 'A' ELSE 'B' END", "type": "STRING"}
    )

    new = json.loads(put.calls.last.request.content)["columns"][-1]
    assert new["expression"].startswith("CASE WHEN") and new["type"] == "STRING"


@respx.mock
async def test_existing_column_with_nothing_to_change_makes_no_write(mcp_server):
    put = _mock_dataset()

    out = await _call(mcp_server, {"column_name": "ID"})

    assert out["action"] == "unchanged"
    assert not put.called


@respx.mock
async def test_blank_column_name_is_rejected_before_any_request(mcp_server):
    put = _mock_dataset()

    out = await _call(mcp_server, {"column_name": "   "})

    assert "error" in out
    assert not put.called


@respx.mock
async def test_does_not_require_the_replace_confirmation_flag(mcp_server):
    """Unlike dataset_update(columns=...), this tool is additive, so no confirm_columns_replace."""
    put = _mock_dataset()
    out = await _call(mcp_server, {"column_name": "YAKA_TIPI"})
    assert "error" not in out and put.called

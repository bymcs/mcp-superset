"""The confirm_delete guard on every tool that deletes something.

Ten tools share one contract: without ``confirm_delete=True`` they refuse, say what is about to be lost
(so the caller can make an informed decision) and send NO DELETE; with it they delete exactly the
object asked for. One table drives all ten so a new delete tool, or a refactor of one, cannot quietly
drop the guard. (Users, roles, RLS rules and native filters have richer guards and their own tests.)
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
API = f"{BASE}/api/v1"


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


async def _call(mcp_server, tool, args):
    async with Client(mcp_server) as c:
        return json.loads(_text(await c.call_tool(tool, args)))


def _ok(body):
    return httpx.Response(200, json=body)


# (tool, arguments, DELETE path, {GET path -> body used to describe the object}, text the refusal must contain)
CASES = {
    "chart": (
        "superset_chart_delete",
        {"chart_id": 5},
        "/chart/5",
        {"/chart/5": {"result": {"slice_name": "Sales", "dashboards": [{"dashboard_title": "Exec"}]}}},
        ["Sales", "Exec"],
    ),
    "dashboard": (
        "superset_dashboard_delete",
        {"dashboard_id": 5},
        "/dashboard/5",
        {
            "/dashboard/5": {"result": {"dashboard_title": "Exec", "slug": "exec", "published": True}},
            "/dashboard/5/charts": {"result": [{"id": 1}, {"id": 2}]},
        },
        ["Exec", "slug=exec", "published=True", "charts=2"],
    ),
    "database": (
        "superset_database_delete",
        {"database_id": 5},
        "/database/5",
        {
            "/database/5": {"result": {"database_name": "warehouse"}},
            "/database/5/related_objects/": {"charts": {"count": 3}, "dashboards": {"count": 2}},
        },
        ["warehouse", "3 charts", "2 dashboards"],
    ),
    "dataset": (
        "superset_dataset_delete",
        {"dataset_id": 5},
        "/dataset/5",
        {
            "/dataset/5": {"result": {"table_name": "orders"}},
            "/dataset/5/related_objects/": {"charts": {"count": 3}, "dashboards": {"count": 2}},
        },
        ["orders", "3 charts", "2 dashboards"],
    ),
    "group": (
        "superset_group_delete",
        {"group_id": 5},
        "/security/groups/5",
        {
            "/security/groups/5": {
                "result": {"name": "emea", "roles": [{"name": "Gamma"}], "users": [{"username": "ada"}]}
            }
        },
        ["emea", "Gamma", "ada"],
    ),
    "saved_query": (
        "superset_saved_query_delete",
        {"saved_query_id": 5},
        "/saved_query/5",
        {"/saved_query/5": {"result": {"label": "monthly", "database": {"database_name": "warehouse"}}}},
        ["monthly", "warehouse"],
    ),
    "report": (
        "superset_report_delete",
        {"report_id": 5},
        "/report/5",
        {"/report/5": {"result": {"name": "Weekly", "type": "Report", "active": True}}},
        ["Weekly", "Report", "active=True"],
    ),
    "annotation_layer": (
        "superset_annotation_layer_delete",
        {"annotation_layer_id": 5},
        "/annotation_layer/5",
        {
            "/annotation_layer/5": {"result": {"name": "Releases"}},
            "/annotation_layer/5/annotation/": {"count": 12},
        },
        ["Releases", "12 annotations"],
    ),
    "annotation": (
        "superset_annotation_delete",
        {"annotation_layer_id": 5, "annotation_id": 9},
        "/annotation_layer/5/annotation/9",
        {"/annotation_layer/5/annotation/9": {"result": {"short_descr": "v1 shipped"}}},
        ["v1 shipped", "layer 5"],
    ),
    "tag": (
        "superset_tag_delete",
        {"tag_id": 5},
        "/tag/5",
        {"/tag/5": {"result": {"name": "prod"}}},
        ["prod"],
    ),
}
IDS = list(CASES)


def _mock(case, lookups_fail=False):
    """Mock CSRF, the descriptive GETs (or make them fail) and the DELETE route; return the DELETE route."""
    _tool, _args, delete_path, lookups, _expected = case
    respx.get(f"{API}/security/csrf_token/").mock(return_value=_ok({"result": "csrf"}))
    for path, body in lookups.items():
        response = httpx.Response(500, json={"message": "boom"}) if lookups_fail else _ok(body)
        respx.get(f"{API}{path}").mock(return_value=response)
    return respx.delete(f"{API}{delete_path}").mock(return_value=_ok({"message": "OK"}))


@respx.mock
@pytest.mark.parametrize("name", IDS)
async def test_without_confirmation_nothing_is_deleted_and_the_refusal_says_what_would_be_lost(mcp_server, name):
    case = CASES[name]
    delete = _mock(case)

    out = await _call(mcp_server, case[0], case[1])  # confirm_delete left at its default

    assert out["error"].startswith("REJECTED")
    assert "confirm_delete=True" in out["error"]  # tells the caller how to proceed
    for detail in case[4]:
        assert detail in out["error"], f"refusal does not mention {detail!r}: {out['error']}"
    assert not delete.called


@respx.mock
@pytest.mark.parametrize("name", IDS)
async def test_an_explicit_false_is_the_same_as_the_default(mcp_server, name):
    case = CASES[name]
    delete = _mock(case)

    out = await _call(mcp_server, case[0], {**case[1], "confirm_delete": False})

    assert out["error"].startswith("REJECTED")
    assert not delete.called


@respx.mock
@pytest.mark.parametrize("name", IDS)
async def test_the_refusal_still_holds_when_the_object_cannot_be_described(mcp_server, name):
    """If the lookups that build the warning fail, the delete is still refused (the guard fails closed)."""
    case = CASES[name]
    delete = _mock(case, lookups_fail=True)

    out = await _call(mcp_server, case[0], case[1])

    assert out["error"].startswith("REJECTED")
    assert not delete.called


@respx.mock
@pytest.mark.parametrize("name", IDS)
async def test_with_confirmation_exactly_the_requested_object_is_deleted(mcp_server, name):
    case = CASES[name]
    delete = _mock(case)

    out = await _call(mcp_server, case[0], {**case[1], "confirm_delete": True})

    assert delete.call_count == 1
    assert out == {"message": "OK"}


# ----------------------------------------------------- group membership removal


GROUP = {
    "result": {
        "name": "emea",
        "users": [{"id": 1}, {"id": 2}, {"id": 3}],
        "roles": [{"id": 10}, {"id": 20}],
    }
}


def _mock_group():
    respx.get(f"{API}/security/csrf_token/").mock(return_value=_ok({"result": "csrf"}))
    respx.get(f"{API}/security/groups/5").mock(return_value=_ok(GROUP))
    return respx.put(f"{API}/security/groups/5").mock(return_value=_ok({"result": {}}))


@respx.mock
async def test_group_remove_users_keeps_everyone_else_and_reports_who_was_not_a_member(mcp_server):
    put = _mock_group()

    out = await _call(mcp_server, "superset_group_remove_users", {"group_id": 5, "user_ids": [2, 99]})

    assert json.loads(put.calls.last.request.content) == {"users": [1, 3]}  # roles are not sent, so not touched
    assert out["removed"] == [2] and out["not_in_group"] == [99] and out["total_users"] == 2


@respx.mock
async def test_group_remove_roles_keeps_the_other_roles_and_never_touches_users(mcp_server):
    put = _mock_group()

    out = await _call(mcp_server, "superset_group_remove_roles", {"group_id": 5, "role_ids": [20, 77]})

    assert json.loads(put.calls.last.request.content) == {"roles": [10]}
    assert out["removed_roles"] == [20] and out["not_in_group"] == [77] and out["total_roles"] == 1

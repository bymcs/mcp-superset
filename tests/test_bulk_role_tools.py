"""Bulk role tools: they rewrite the role list of many users at once, so what they do and, above
all, what they refuse to do (dry-run default, Admin exclusion, last-role guard) must be pinned.

Superset's PUT /security/users/{id} replaces a user's whole role list, so every write here is
"existing roles +/- one", and a mistake locks people out or hands them access they should not have.
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

ADMIN = {"id": 1, "name": "Admin"}
GAMMA = {"id": 10, "name": "Gamma"}
SALES = {"id": 20, "name": "Sales"}

USERS = [
    {"id": 1, "username": "admin", "roles": [ADMIN, SALES]},
    {"id": 2, "username": "alice", "roles": [GAMMA]},
    {"id": 3, "username": "bob", "roles": [GAMMA, SALES]},
    {"id": 4, "username": "carol", "roles": [SALES]},
]


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


def _mock_users(failing_ids=()):
    """Mock the user list, role-name lookups and one PUT route per user. Returns {user_id: put_route}."""
    respx.get(f"{BASE}/api/v1/security/csrf_token/").mock(return_value=httpx.Response(200, json={"result": "csrf"}))
    respx.get(f"{BASE}/api/v1/security/users/").mock(
        return_value=httpx.Response(200, json={"result": USERS, "count": len(USERS)})
    )
    for role in (ADMIN, GAMMA, SALES, {"id": 30, "name": "Support"}):
        respx.get(f"{BASE}/api/v1/security/roles/{role['id']}").mock(
            return_value=httpx.Response(200, json={"result": {"id": role["id"], "name": role["name"]}})
        )
    routes = {}
    for user in USERS:
        status = 500 if user["id"] in failing_ids else 200
        routes[user["id"]] = respx.put(f"{BASE}/api/v1/security/users/{user['id']}").mock(
            return_value=httpx.Response(status, json={"result": {}} if status == 200 else {"message": "boom"})
        )
    return routes


async def _call(mcp_server, tool, args):
    async with Client(mcp_server) as c:
        return json.loads(_text(await c.call_tool(tool, args)))


def _sent_roles(route):
    return sorted(json.loads(route.calls.last.request.content)["roles"])


def _writes(routes):
    return sorted(uid for uid, r in routes.items() if r.called)


# ---------------------------------------------------------------- bulk add


@respx.mock
async def test_add_is_a_dry_run_by_default_and_writes_nothing(mcp_server):
    routes = _mock_users()

    out = await _call(mcp_server, "superset_bulk_user_role_add", {"role_id": 30, "user_ids": [2, 3, 4]})

    assert out["status"] == "dry_run" and out["role"] == "Support"
    assert out["users_to_update"] == 3
    assert [u["username"] for u in out["sample"]] == ["alice", "bob", "carol"]
    assert _writes(routes) == []


@respx.mock
async def test_add_keeps_existing_roles_and_skips_users_who_already_have_it(mcp_server):
    routes = _mock_users()

    out = await _call(
        mcp_server, "superset_bulk_user_role_add", {"role_id": 20, "user_ids": [2, 3, 4], "confirm": True}
    )

    assert out == {"status": "applied", "ok": 1, "fail": 0, "errors": []}
    assert _writes(routes) == [2]  # bob and carol already have Sales: no write at all
    assert _sent_roles(routes[2]) == [10, 20]  # alice keeps Gamma


@respx.mock
async def test_add_excludes_admin_unless_asked(mcp_server):
    routes = _mock_users()
    await _call(mcp_server, "superset_bulk_user_role_add", {"role_id": 30, "user_ids": [1, 2], "confirm": True})
    assert _writes(routes) == [2]  # admin protected by default

    routes = _mock_users()
    await _call(
        mcp_server,
        "superset_bulk_user_role_add",
        {"role_id": 30, "user_ids": [1, 2], "exclude_admin": False, "confirm": True},
    )
    assert _writes(routes) == [1, 2]
    assert _sent_roles(routes[1]) == [1, 20, 30]


@respx.mock
async def test_add_can_select_users_by_their_current_role(mcp_server):
    routes = _mock_users()

    await _call(mcp_server, "superset_bulk_user_role_add", {"role_id": 30, "filter_role_id": 10, "confirm": True})

    assert _writes(routes) == [2, 3]  # exactly the Gamma users
    assert _sent_roles(routes[3]) == [10, 20, 30]


@respx.mock
async def test_add_needs_a_selector(mcp_server):
    routes = _mock_users()

    out = await _call(mcp_server, "superset_bulk_user_role_add", {"role_id": 30, "confirm": True})

    assert "user_ids or filter_role_id" in out["error"]
    assert _writes(routes) == []


@respx.mock
async def test_add_reports_partial_failure_and_keeps_going(mcp_server):
    routes = _mock_users(failing_ids=(2,))

    out = await _call(mcp_server, "superset_bulk_user_role_add", {"role_id": 30, "user_ids": [2, 3], "confirm": True})

    assert out["ok"] == 1 and out["fail"] == 1
    assert [e["user_id"] for e in out["errors"]] == [2]
    assert routes[3].called  # one failure does not stop the rest


# ------------------------------------------------------------- bulk remove


@respx.mock
async def test_remove_is_a_dry_run_by_default(mcp_server):
    routes = _mock_users()

    out = await _call(mcp_server, "superset_bulk_user_role_remove", {"role_id": 20})

    assert out["status"] == "dry_run" and out["role"] == "Sales"
    assert out["users_to_update"] == 2  # bob and carol; admin is protected
    assert _writes(routes) == []


@respx.mock
async def test_remove_drops_only_that_role_and_never_the_last_one(mcp_server):
    routes = _mock_users()

    out = await _call(mcp_server, "superset_bulk_user_role_remove", {"role_id": 20, "confirm": True})

    assert out["ok"] == 1 and out["fail"] == 1
    assert _sent_roles(routes[3]) == [10]  # bob keeps Gamma
    assert not routes[4].called  # carol's only role: refused, not written
    assert out["errors"] == [{"user_id": 4, "error": "Cannot remove last role"}]
    assert not routes[1].called  # admin untouched


@respx.mock
async def test_remove_with_user_ids_touches_only_those_users(mcp_server):
    routes = _mock_users()

    await _call(mcp_server, "superset_bulk_user_role_remove", {"role_id": 20, "user_ids": [3], "confirm": True})

    assert _writes(routes) == [3]


@respx.mock
async def test_remove_can_include_admin_when_asked(mcp_server):
    routes = _mock_users()

    await _call(
        mcp_server,
        "superset_bulk_user_role_remove",
        {"role_id": 20, "user_ids": [1], "exclude_admin": False, "confirm": True},
    )

    assert _sent_roles(routes[1]) == [1]  # still Admin, just no Sales


# ------------------------------------------------------------ bulk replace


@respx.mock
async def test_replace_is_a_dry_run_by_default(mcp_server):
    routes = _mock_users()

    out = await _call(mcp_server, "superset_bulk_user_role_replace", {"old_role_id": 10, "new_role_id": 20})

    assert out["status"] == "dry_run"
    assert (out["old_role"], out["new_role"]) == ("Gamma", "Sales")
    assert out["users_to_update"] == 2  # alice and bob
    assert _writes(routes) == []


@respx.mock
async def test_replace_swaps_the_role_without_duplicating_an_existing_one(mcp_server):
    routes = _mock_users()

    out = await _call(
        mcp_server, "superset_bulk_user_role_replace", {"old_role_id": 10, "new_role_id": 20, "confirm": True}
    )

    assert out["ok"] == 2 and out["fail"] == 0
    assert _sent_roles(routes[2]) == [20]  # alice: Gamma -> Sales
    assert _sent_roles(routes[3]) == [20]  # bob already had Sales: no duplicate
    assert not routes[4].called and not routes[1].called


@respx.mock
async def test_replace_excludes_admin_by_default(mcp_server):
    routes = _mock_users()

    await _call(mcp_server, "superset_bulk_user_role_replace", {"old_role_id": 20, "new_role_id": 30, "confirm": True})

    assert 1 not in _writes(routes)
    assert _writes(routes) == [3, 4]
    assert _sent_roles(routes[3]) == [10, 30]


# ----------------------------------------------------- role_copy_permissions


def _mock_role_permissions():
    respx.get(f"{BASE}/api/v1/security/csrf_token/").mock(return_value=httpx.Response(200, json={"result": "csrf"}))
    # Source permissions arrive as dicts or bare ints depending on the Superset build.
    respx.get(f"{BASE}/api/v1/security/roles/20/permissions/").mock(
        return_value=httpx.Response(200, json={"result": [{"id": 5}, {"id": 6}, 7]})
    )
    respx.get(f"{BASE}/api/v1/security/roles/30/permissions/").mock(
        return_value=httpx.Response(200, json={"result": [{"id": 1}]})
    )
    for role in (SALES, {"id": 30, "name": "Support"}):
        respx.get(f"{BASE}/api/v1/security/roles/{role['id']}").mock(
            return_value=httpx.Response(200, json={"result": {"name": role["name"]}})
        )
    return respx.post(f"{BASE}/api/v1/security/roles/30/permissions").mock(
        return_value=httpx.Response(200, json={"result": {}})
    )


@respx.mock
async def test_copy_permissions_is_a_dry_run_by_default(mcp_server):
    post = _mock_role_permissions()

    out = await _call(mcp_server, "superset_role_copy_permissions", {"source_role_id": 20, "target_role_id": 30})

    assert out["status"] == "dry_run"
    assert (out["source"], out["target"]) == ("Sales", "Support")
    assert out["permissions_to_copy"] == 3
    assert out["target_current_permissions"] == 1
    assert not post.called


@respx.mock
async def test_copy_permissions_replaces_the_target_with_the_source_permission_ids(mcp_server):
    post = _mock_role_permissions()

    out = await _call(
        mcp_server, "superset_role_copy_permissions", {"source_role_id": 20, "target_role_id": 30, "confirm": True}
    )

    assert json.loads(post.calls.last.request.content) == {"permission_view_menu_ids": [5, 6, 7]}
    assert out["status"] == "applied" and out["permissions_set"] == 3

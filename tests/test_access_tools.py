"""Tools that decide who can see what: users, roles, role permissions, RLS rules and the dashboard
grant/revoke helpers. A wrong write here silently widens or removes access, so the guards (confirm
flags, protected roles, replace-everything warnings, Base-RLS refusal, dry-runs) are pinned.

Guards that depend on a lookup (who am I, what is this role called) fail CLOSED: if the lookup errors
or comes back unusable, the destructive call is refused instead of going ahead unguarded.
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


def _ok(body=None):
    return httpx.Response(200, json=body if body is not None else {"result": {}})


def _csrf():
    respx.get(f"{API}/security/csrf_token/").mock(return_value=_ok({"result": "csrf"}))


def _sent(route):
    return json.loads(route.calls.last.request.content)


# ====================================================================== users


@respx.mock
async def test_user_create_sends_the_account_and_only_sends_roles_when_given(mcp_server):
    _csrf()
    post = respx.post(f"{API}/security/users/").mock(return_value=_ok({"id": 9}))
    args = {"first_name": "Ada", "last_name": "L", "username": "ada", "email": "a@x.io", "password": "pw"}

    await _call(mcp_server, "superset_user_create", args)
    assert _sent(post) == {
        **{k: args[k] for k in args},
        "active": True,
    }  # no 'roles' key: Superset's default role applies

    await _call(mcp_server, "superset_user_create", {**args, "roles": [3, 4], "active": False})
    assert _sent(post)["roles"] == [3, 4] and _sent(post)["active"] is False


@respx.mock
async def test_user_update_refuses_to_replace_roles_without_confirmation(mcp_server):
    _csrf()
    respx.get(f"{API}/security/users/7").mock(
        return_value=_ok({"result": {"roles": [{"id": 10, "name": "Gamma"}, {"id": 20, "name": "Sales"}]}})
    )
    put = respx.put(f"{API}/security/users/7").mock(return_value=_ok())

    out = await _call(mcp_server, "superset_user_update", {"user_id": 7, "roles": [10]})

    assert (
        "REJECTED" in out["error"] and "Gamma" in out["error"] and "Sales" in out["error"]
    )  # shows what would be lost
    assert not put.called


@respx.mock
async def test_user_update_sends_only_the_fields_that_were_given(mcp_server):
    _csrf()
    put = respx.put(f"{API}/security/users/7").mock(return_value=_ok())

    await _call(mcp_server, "superset_user_update", {"user_id": 7, "email": "new@x.io", "active": False})
    assert _sent(put) == {"email": "new@x.io", "active": False}  # no roles -> no confirmation needed, none sent

    await _call(mcp_server, "superset_user_update", {"user_id": 7, "roles": [10, 20], "confirm_roles_replace": True})
    assert _sent(put) == {"roles": [10, 20]}


@respx.mock
async def test_user_delete_is_rejected_without_confirmation_and_names_the_user(mcp_server):
    _csrf()
    respx.get(f"{API}/security/users/7").mock(
        return_value=_ok({"result": {"username": "ada", "roles": [{"id": 1, "name": "Admin"}]}})
    )
    delete = respx.delete(f"{API}/security/users/7").mock(return_value=_ok())

    out = await _call(mcp_server, "superset_user_delete", {"user_id": 7})

    assert "REJECTED" in out["error"] and "ada" in out["error"] and "Admin" in out["error"]
    assert not delete.called


@respx.mock
async def test_user_delete_refuses_to_delete_the_account_the_server_runs_as(mcp_server):
    _csrf()
    respx.get(f"{API}/me/").mock(return_value=_ok({"result": {"pk": 7, "username": "mcp_service"}}))
    delete = respx.delete(f"{API}/security/users/7").mock(return_value=_ok())

    out = await _call(mcp_server, "superset_user_delete", {"user_id": 7, "confirm_delete": True})

    assert "BLOCKED" in out["error"] and "mcp_service" in out["error"]
    assert not delete.called


@respx.mock
async def test_user_delete_removes_another_user_once_confirmed(mcp_server):
    _csrf()
    respx.get(f"{API}/me/").mock(return_value=_ok({"result": {"pk": 1, "username": "mcp_service"}}))
    delete = respx.delete(f"{API}/security/users/7").mock(return_value=_ok({"message": "OK"}))

    out = await _call(mcp_server, "superset_user_delete", {"user_id": 7, "confirm_delete": True})

    assert delete.called and out == {"message": "OK"}


@respx.mock
@pytest.mark.parametrize(
    "me_response",
    [
        httpx.Response(500, json={"message": "boom"}),  # the lookup errors
        httpx.Response(200, json={"result": {}}),  # ...or answers without saying who we are
        httpx.Response(200, json={}),
    ],
    ids=["me-errors", "me-empty-result", "me-no-result"],
)
async def test_user_delete_does_not_proceed_when_it_cannot_tell_who_it_is(mcp_server, me_response):
    """Deleting the service account locks out the whole MCP server, so an unverifiable identity blocks the delete."""
    _csrf()
    respx.get(f"{API}/me/").mock(return_value=me_response)
    delete = respx.delete(f"{API}/security/users/7").mock(return_value=_ok())

    out = await _call(mcp_server, "superset_user_delete", {"user_id": 7, "confirm_delete": True})

    assert "BLOCKED" in out["error"] and "service account" in out["error"]
    assert not delete.called


# ====================================================================== roles


@respx.mock
async def test_role_create_and_rename_send_just_the_name(mcp_server):
    _csrf()
    post = respx.post(f"{API}/security/roles/").mock(return_value=_ok({"id": 5}))
    put = respx.put(f"{API}/security/roles/5").mock(return_value=_ok())

    await _call(mcp_server, "superset_role_create", {"name": "Reader"})
    await _call(mcp_server, "superset_role_update", {"role_id": 5, "name": "Viewer"})

    assert _sent(post) == {"name": "Reader"}
    assert _sent(put) == {"name": "Viewer"}


@respx.mock
@pytest.mark.parametrize(
    "name",
    ["Admin", "Alpha", "Gamma", "Public", "sql_lab", "no_access", "la_report_x", "la_region_eu", "la_developer_1"],
)
async def test_role_delete_never_deletes_a_protected_role_even_when_confirmed(mcp_server, name):
    _csrf()
    respx.get(f"{API}/security/roles/5").mock(return_value=_ok({"result": {"name": name}}))
    delete = respx.delete(f"{API}/security/roles/5").mock(return_value=_ok())

    out = await _call(mcp_server, "superset_role_delete", {"role_id": 5, "confirm_delete": True})

    assert "BLOCKED" in out["error"] and name in out["error"]
    assert not delete.called


@respx.mock
async def test_role_delete_needs_confirmation_then_deletes_an_ordinary_role(mcp_server):
    _csrf()
    respx.get(f"{API}/security/roles/5").mock(return_value=_ok({"result": {"name": "Reader"}}))
    delete = respx.delete(f"{API}/security/roles/5").mock(return_value=_ok({"message": "OK"}))

    out = await _call(mcp_server, "superset_role_delete", {"role_id": 5})
    assert "REJECTED" in out["error"] and "Reader" in out["error"]
    assert not delete.called

    await _call(mcp_server, "superset_role_delete", {"role_id": 5, "confirm_delete": True})
    assert delete.called


@respx.mock
@pytest.mark.parametrize(
    "lookup",
    [
        httpx.Response(500, json={"message": "boom"}),  # the lookup errors
        httpx.Response(404, json={"message": "not found"}),
        httpx.Response(200, json={"result": {}}),  # ...or answers without a name
        httpx.Response(200, json={"result": {"name": ""}}),
    ],
    ids=["lookup-500", "lookup-404", "no-name", "empty-name"],
)
@pytest.mark.parametrize("confirm", [True, False])
async def test_role_delete_refuses_when_it_cannot_tell_which_role_this_is(mcp_server, lookup, confirm):
    """Without the name there is no way to know it is not Admin, so nothing is deleted, confirmed or not."""
    _csrf()
    respx.get(f"{API}/security/roles/1").mock(return_value=lookup)
    delete = respx.delete(f"{API}/security/roles/1").mock(return_value=_ok())

    out = await _call(mcp_server, "superset_role_delete", {"role_id": 1, "confirm_delete": confirm})

    assert "BLOCKED" in out["error"] and "role ID=1" in out["error"]
    assert not delete.called


@respx.mock
async def test_role_permission_add_replaces_everything_so_it_demands_confirmation(mcp_server):
    _csrf()
    post = respx.post(f"{API}/security/roles/5/permissions").mock(return_value=_ok())

    out = await _call(mcp_server, "superset_role_permission_add", {"role_id": 5, "permission_view_menu_ids": [1, 2]})
    assert "REJECTED" in out["error"] and "REPLACES ALL" in out["error"]
    assert not post.called

    await _call(
        mcp_server,
        "superset_role_permission_add",
        {"role_id": 5, "permission_view_menu_ids": [1, 2], "confirm_full_replace": True},
    )
    assert _sent(post) == {"permission_view_menu_ids": [1, 2]}


# ======================================================================== RLS


@respx.mock
async def test_rls_create_refuses_base_rules_because_they_apply_to_everyone(mcp_server):
    _csrf()
    post = respx.post(f"{API}/rowlevelsecurity/").mock(return_value=_ok({"id": 1}))

    out = await _call(
        mcp_server,
        "superset_rls_create",
        {"name": "n", "clause": "1=0", "tables": [7], "roles": [5], "filter_type": "Base"},
    )

    assert "REJECTED" in out["error"] and "Base" in out["error"]
    assert not post.called


@respx.mock
async def test_rls_create_sends_a_regular_rule_with_optional_fields_only_when_given(mcp_server):
    _csrf()
    post = respx.post(f"{API}/rowlevelsecurity/").mock(return_value=_ok({"id": 1}))
    base = {"name": "firma", "clause": "COMPANY = 'A'", "tables": [7, 8], "roles": [5]}

    await _call(mcp_server, "superset_rls_create", base)
    assert _sent(post) == {**base, "filter_type": "Regular"}

    await _call(mcp_server, "superset_rls_create", {**base, "group_key": "g", "description": "d"})
    assert _sent(post)["group_key"] == "g" and _sent(post)["description"] == "d"


@respx.mock
@pytest.mark.parametrize(("args", "missing"), [({"roles": [5]}, "tables"), ({"tables": [7]}, "roles")])
async def test_rls_update_refuses_roles_without_tables_and_the_reverse(mcp_server, args, missing):
    """Superset's PUT wipes whichever of roles/tables is left out, which would silently drop or open access."""
    _csrf()
    put = respx.put(f"{API}/rowlevelsecurity/3").mock(return_value=_ok())

    out = await _call(mcp_server, "superset_rls_update", {"rls_id": 3, **args})

    assert "REJECTED" in out["error"] and missing in out["error"]
    assert not put.called


@respx.mock
async def test_rls_update_sends_roles_and_tables_together_and_nothing_that_was_not_given(mcp_server):
    _csrf()
    put = respx.put(f"{API}/rowlevelsecurity/3").mock(return_value=_ok())

    await _call(mcp_server, "superset_rls_update", {"rls_id": 3, "roles": [5], "tables": [7], "clause": "x = 1"})
    assert _sent(put) == {"roles": [5], "tables": [7], "clause": "x = 1"}

    await _call(mcp_server, "superset_rls_update", {"rls_id": 3, "name": "renamed"})
    assert _sent(put) == {"name": "renamed"}  # neither roles nor tables -> neither sent, nothing wiped


@respx.mock
async def test_rls_delete_warns_with_the_rule_details_and_needs_confirmation(mcp_server):
    _csrf()
    respx.get(f"{API}/rowlevelsecurity/3").mock(
        return_value=_ok(
            {
                "result": {
                    "name": "deny-all",
                    "clause": "1=0",
                    "roles": [{"name": "firma__A"}],
                    "tables": [{"table_name": "orders"}],
                }
            }
        )
    )
    delete = respx.delete(f"{API}/rowlevelsecurity/3").mock(return_value=_ok({"message": "OK"}))

    out = await _call(mcp_server, "superset_rls_delete", {"rls_id": 3})
    assert "REJECTED" in out["error"]
    for detail in ("deny-all", "1=0", "firma__A", "orders"):
        assert detail in out["error"]
    assert not delete.called

    await _call(mcp_server, "superset_rls_delete", {"rls_id": 3, "confirm_delete": True})
    assert delete.called


@respx.mock
async def test_rls_delete_is_still_rejected_when_the_rule_cannot_be_read(mcp_server):
    _csrf()
    respx.get(f"{API}/rowlevelsecurity/3").mock(return_value=httpx.Response(500, json={"message": "boom"}))
    delete = respx.delete(f"{API}/rowlevelsecurity/3").mock(return_value=_ok())

    out = await _call(mcp_server, "superset_rls_delete", {"rls_id": 3})

    assert "REJECTED" in out["error"]  # unlike the two guards above, this one fails closed
    assert not delete.called


# ============================================================= grant / revoke

DASHBOARD = {"dashboard_title": "Sales", "slug": "sales"}
DATASETS = [
    {"id": 7, "schema": "public", "table_name": "orders"},
    {"id": 8, "schema": "public", "table_name": "customers"},
]


def _permission_resources(*dataset_perms):
    """Entries of /security/permissions-resources/: datasource_access ones plus noise that must be ignored."""
    items = [
        {
            "id": perm_id,
            "permission": {"name": "datasource_access"},
            "view_menu": {"name": f"[main].[t{ds_id}](id:{ds_id})"},
        }
        for ds_id, perm_id in dataset_perms
    ]
    items += [
        {"id": 900, "permission": {"name": "can_read"}, "view_menu": {"name": "[main].[t7](id:7)"}},  # wrong permission
        {"id": 901, "permission": {"name": "datasource_access"}, "view_menu": {"name": "[main].[other](id:99)"}},
        {"id": 902, "permission": {"name": "datasource_access"}, "view_menu": {"name": "no id here"}},
    ]
    return {"result": items}


def _mock_access(role_perm_ids, datasets=DATASETS, perms=((7, 101), (8, 102)), rls_tables=(7,)):
    _csrf()
    respx.get(f"{API}/dashboard/31").mock(return_value=_ok({"result": DASHBOARD}))
    respx.get(f"{API}/dashboard/31/datasets").mock(return_value=_ok({"result": datasets}))
    respx.get(f"{API}/security/roles/5").mock(return_value=_ok({"result": {"name": "Reader"}}))
    respx.get(f"{API}/security/roles/5/permissions/").mock(
        return_value=_ok({"result": [{"id": i} for i in role_perm_ids]})
    )
    respx.get(f"{API}/security/permissions-resources/").mock(return_value=_ok(_permission_resources(*perms)))
    respx.get(f"{API}/rowlevelsecurity/").mock(
        return_value=_ok({"result": [{"id": 1, "tables": [{"id": t} for t in rls_tables]}], "count": 1})
    )
    return respx.post(f"{API}/security/roles/5/permissions").mock(return_value=_ok())


@respx.mock
async def test_grant_is_a_dry_run_by_default_and_reports_the_plan_and_rls_gaps(mcp_server):
    post = _mock_access(role_perm_ids=[1, 101])  # already has dataset 7, lacks dataset 8

    out = await _call(mcp_server, "superset_dashboard_grant_role_access", {"dashboard_id": 31, "role_id": 5})

    assert out["status"] == "dry_run"
    report = out["result"]
    assert "Already has access" in report and "public.orders" in report
    assert "Will be added" in report and "public.customers (dataset=8, perm=102)" in report
    assert "no RLS rules" in report and "public.customers" in report.split("RLS warnings:")[1]  # 8 has no RLS rule
    assert "orders" not in report.split("RLS warnings:")[1]  # 7 does
    assert not post.called


@respx.mock
async def test_grant_adds_only_the_missing_permissions_to_the_existing_ones(mcp_server):
    post = _mock_access(role_perm_ids=[1, 101])

    out = await _call(
        mcp_server, "superset_dashboard_grant_role_access", {"dashboard_id": 31, "role_id": 5, "confirm_grant": True}
    )

    assert _sent(post) == {"permission_view_menu_ids": [1, 101, 102]}  # nothing the role already had is lost
    assert out["status"] == "applied" and out["added_count"] == 1 and out["total_permissions"] == 3


@respx.mock
async def test_grant_does_nothing_when_the_role_already_has_everything(mcp_server):
    post = _mock_access(role_perm_ids=[101, 102])

    out = await _call(
        mcp_server, "superset_dashboard_grant_role_access", {"dashboard_id": 31, "role_id": 5, "confirm_grant": True}
    )

    assert out["status"] == "nothing_to_do"
    assert not post.called


@respx.mock
async def test_grant_still_applies_what_it_found_and_reports_a_dataset_without_a_permission(mcp_server):
    datasets = [*DATASETS, {"id": 9, "schema": "public", "table_name": "fresh"}]
    post = _mock_access(role_perm_ids=[], datasets=datasets)  # dataset 9 has no datasource_access yet

    out = await _call(
        mcp_server, "superset_dashboard_grant_role_access", {"dashboard_id": 31, "role_id": 5, "confirm_grant": True}
    )

    assert _sent(post) == {"permission_view_menu_ids": [101, 102]}
    assert "datasource_access not found for datasets" in out["result"] and "public.fresh" in out["result"]


@respx.mock
@pytest.mark.parametrize(
    ("missing", "expected"),
    [("dashboard", "Dashboard ID=31 not found"), ("role", "Role ID=5 not found"), ("datasets", "has no datasets")],
)
async def test_grant_stops_with_an_error_and_writes_nothing_when_something_is_missing(mcp_server, missing, expected):
    post = _mock_access(role_perm_ids=[])
    if missing == "dashboard":
        respx.get(f"{API}/dashboard/31").mock(return_value=httpx.Response(404, json={"message": "nope"}))
    elif missing == "role":
        respx.get(f"{API}/security/roles/5").mock(return_value=httpx.Response(404, json={"message": "nope"}))
    else:
        respx.get(f"{API}/dashboard/31/datasets").mock(return_value=_ok({"result": []}))

    out = await _call(
        mcp_server, "superset_dashboard_grant_role_access", {"dashboard_id": 31, "role_id": 5, "confirm_grant": True}
    )

    assert expected in out["error"]
    assert not post.called


@respx.mock
async def test_revoke_is_a_dry_run_by_default(mcp_server):
    post = _mock_access(role_perm_ids=[1, 101, 102, 200])

    out = await _call(mcp_server, "superset_dashboard_revoke_role_access", {"dashboard_id": 31, "role_id": 5})

    assert out["status"] == "dry_run"
    assert "perm=101" in out["result"] and "perm=102" in out["result"] and "perm=200" not in out["result"]
    assert not post.called


@respx.mock
async def test_revoke_removes_only_this_dashboards_dataset_permissions(mcp_server):
    post = _mock_access(role_perm_ids=[1, 101, 102, 200])

    out = await _call(
        mcp_server,
        "superset_dashboard_revoke_role_access",
        {"dashboard_id": 31, "role_id": 5, "confirm_revoke": True},
    )

    assert _sent(post) == {"permission_view_menu_ids": [1, 200]}  # unrelated permissions survive
    assert out["status"] == "applied" and out["removed_count"] == 2


@respx.mock
async def test_revoke_does_nothing_when_the_role_never_had_access(mcp_server):
    post = _mock_access(role_perm_ids=[1, 200])

    out = await _call(
        mcp_server,
        "superset_dashboard_revoke_role_access",
        {"dashboard_id": 31, "role_id": 5, "confirm_revoke": True},
    )

    assert out["status"] == "nothing_to_do"
    assert not post.called


@respx.mock
async def test_revoke_reports_when_no_datasource_permission_exists_for_the_datasets(mcp_server):
    post = _mock_access(role_perm_ids=[1], perms=())

    out = await _call(
        mcp_server,
        "superset_dashboard_revoke_role_access",
        {"dashboard_id": 31, "role_id": 5, "confirm_revoke": True},
    )

    assert "No datasource_access found" in out["error"]
    assert not post.called

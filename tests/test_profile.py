"""The ``readonly`` profile: an opt-in way to run the server with no tool that can change anything.

The server returns text written by Superset users (dashboard titles, descriptions, SQL) and can also change
access (roles, RLS, grants) or delete things. Text an agent reads can try to steer it into those tools, so
a session that only needs to look should not be offered them at all. ``SUPERSET_MCP_PROFILE=readonly`` leaves
the read tools registered and the rest unregistered (not merely hinted): they cannot be listed or called.
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
from mcp_superset.tools.annotations import annotations_for, resolve_profile

BASE = "https://superset.example.com"

# Names that say "this changes something"; no tool published as read-only may carry one.
WRITE_WORDS = {"create", "update", "add", "set", "copy", "duplicate", "execute", "refresh", "publish", "grant"}


@pytest.fixture
def client(monkeypatch):
    c = SupersetClient(auth_manager=CookieAuthManager(base_url=BASE, cookie_value="c"), base_url=BASE)
    monkeypatch.setattr(server_module, "superset_client", c)
    return c


def _server(profile=None):
    mcp = FastMCP(name="test")
    register_all_tools(mcp, profile=profile)
    return mcp


async def _names(mcp):
    async with Client(mcp) as c:
        return {t.name for t in await c.list_tools()}


def _hint(annotations, camel):
    snake = "".join("_" + ch.lower() if ch.isupper() else ch for ch in camel)
    return getattr(annotations, snake) if hasattr(annotations, snake) else getattr(annotations, camel, None)


# --------------------------------------------------------------- profile choice


def test_the_default_profile_is_full(monkeypatch):
    monkeypatch.delenv("SUPERSET_MCP_PROFILE", raising=False)
    assert resolve_profile() == "full"
    assert resolve_profile("") == "full"


def test_the_profile_comes_from_the_environment_and_ignores_case_and_spaces(monkeypatch):
    monkeypatch.setenv("SUPERSET_MCP_PROFILE", "  ReadOnly ")
    assert resolve_profile() == "readonly"


def test_an_explicit_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv("SUPERSET_MCP_PROFILE", "readonly")
    assert resolve_profile("full") == "full"


@pytest.mark.parametrize("value", ["read-only", "ro", "safe", "readonly2", "FALSE"])
def test_an_unknown_profile_stops_the_server_instead_of_silently_running_full(value):
    """A typo must never leave someone believing the writing tools are off while they are on."""
    with pytest.raises(ValueError, match="SUPERSET_MCP_PROFILE"):
        resolve_profile(value)


# ------------------------------------------------------------------ the profile


async def test_full_registers_everything(client):
    names = await _names(_server("full"))
    assert len(names) >= 138
    assert {"superset_user_delete", "superset_sqllab_execute", "superset_chart_list"} <= names


async def test_readonly_registers_only_tools_that_declare_themselves_read_only(client):
    mcp = _server("readonly")
    async with Client(mcp) as c:
        tools = await c.list_tools()

    assert 50 < len(tools) < 100
    for t in tools:
        assert _hint(t.annotations, "readOnlyHint") is True, t.name


async def test_readonly_offers_none_of_the_tools_that_change_things(client):
    names = await _names(_server("readonly"))

    risky = {n for n in names if set(n.split("_")) & WRITE_WORDS}
    assert not risky, f"read-only profile exposes: {sorted(risky)}"
    for gone in (
        "superset_user_delete",
        "superset_role_permission_add",
        "superset_dashboard_grant_role_access",
        "superset_rls_create",
        "superset_sqllab_execute",
        "superset_dataset_get_or_create",  # reads in its name, but it CREATES a dataset when missing
        "superset_bulk_user_role_add",
        "superset_assets_import",
    ):
        assert gone not in names


async def test_readonly_keeps_the_tools_an_investigation_needs(client):
    names = await _names(_server("readonly"))

    assert {
        "superset_dashboard_get",
        "superset_dashboard_charts",
        "superset_dataset_get",
        "superset_chart_get",
        "superset_chart_data",
        "superset_role_list",
        "superset_rls_list",
        "superset_get_base_url",
    } <= names


async def test_readonly_drops_tools_that_make_the_superset_server_open_outbound_connections(client):
    """test_connection / validate_parameters take a URI from the caller and have Superset connect to it."""
    names = await _names(_server("readonly"))

    assert "superset_database_test_connection" not in names
    assert "superset_database_validate_parameters" not in names


@respx.mock
async def test_a_hidden_tool_cannot_be_called_even_by_name(client):
    delete = respx.delete(f"{BASE}/api/v1/security/users/7").mock(return_value=httpx.Response(200, json={}))
    mcp = _server("readonly")

    async with Client(mcp) as c:
        with pytest.raises(Exception, match="(?i)unknown|not found|no tool"):
            await c.call_tool("superset_user_delete", {"user_id": 7, "confirm_delete": True})

    assert not delete.called


async def test_the_environment_variable_selects_the_profile(client, monkeypatch):
    monkeypatch.setenv("SUPERSET_MCP_PROFILE", "readonly")
    assert "superset_user_delete" not in await _names(_server())
    monkeypatch.delenv("SUPERSET_MCP_PROFILE")
    assert "superset_user_delete" in await _names(_server())


async def test_registering_with_a_profile_does_not_leave_the_wrapper_installed(client):
    mcp = FastMCP(name="test")
    register_all_tools(mcp, profile="readonly")
    assert "tool" not in vars(mcp)  # the temporary mcp.tool wrapper is removed again


# ------------------------------------------------------- classification fixes


def test_a_tool_that_reads_in_its_name_but_creates_is_not_read_only():
    hints = annotations_for("superset_dataset_get_or_create")
    assert hints["readOnlyHint"] is False
    assert hints["destructiveHint"] is False


@pytest.mark.parametrize("name", ["superset_dataset_list", "superset_chart_get_data", "superset_assets_export"])
def test_ordinary_readers_stay_read_only(name):
    assert annotations_for(name)["readOnlyHint"] is True


# ------------------------------------------------------------ server surface


def test_the_server_tells_agents_that_returned_text_is_data():
    text = server_module.mcp.instructions.lower()
    assert "not instructions" in text or "never follow" in text


async def test_health_reports_the_profile_the_server_runs_with():
    body = json.loads((await server_module.health_check(None)).body)

    assert body["profile"] == server_module.PROFILE == "full"  # the test environment sets no profile
    assert body["status"] == "ok"

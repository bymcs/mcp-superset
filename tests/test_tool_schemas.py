"""Schema-level guarantees every client relies on, checked against the fully registered server.

Parameter descriptions are what an agent reads to choose arguments. They come from the ``Args:``
section of each tool's docstring, which FastMCP only turns into schema descriptions from 3.2.4 on;
older releases silently publish bare parameters. These tests keep that from regressing unnoticed.
"""

import pytest
from fastmcp import Client, FastMCP

import mcp_superset.server as server_module
from mcp_superset.auth import CookieAuthManager
from mcp_superset.client import SupersetClient
from mcp_superset.tools import register_all_tools

BASE = "https://superset.example.com"


@pytest.fixture
def mcp_server(monkeypatch):
    client = SupersetClient(auth_manager=CookieAuthManager(base_url=BASE, cookie_value="c"), base_url=BASE)
    monkeypatch.setattr(server_module, "superset_client", client)
    mcp = FastMCP(name="test")
    register_all_tools(mcp)
    return mcp


async def _tools(mcp_server):
    async with Client(mcp_server) as c:
        return await c.list_tools()


def _input_schema(tool) -> dict:
    """fastmcp 3.x exposes ``inputSchema``; 4.x renamed it to ``input_schema`` and warns on the old name."""
    schema = tool.input_schema if hasattr(tool, "input_schema") else tool.inputSchema
    return schema or {}


async def test_every_tool_parameter_has_a_description(mcp_server):
    missing = {}
    for tool in await _tools(mcp_server):
        props = _input_schema(tool).get("properties", {})
        bare = [name for name, schema in props.items() if not schema.get("description")]
        if bare:
            missing[tool.name] = bare
    assert not missing, (
        f"{len(missing)} tools publish parameters without a description (docstring 'Args:' is only "
        f"parsed by fastmcp>=3.2.4, or the docstring lacks an entry): {dict(list(missing.items())[:5])}"
    )

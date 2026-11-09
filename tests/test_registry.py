"""Whole-catalogue checks on the fully registered server.

Per-tool tests cover behaviour; these cover the contract every one of the ~140 tools must meet so a
new tool cannot ship with a clashing name, no description, missing safety hints or an out-of-date README.
"""

import re
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

import mcp_superset.server as server_module
from mcp_superset.auth import CookieAuthManager
from mcp_superset.client import SupersetClient
from mcp_superset.tools import register_all_tools

BASE = "https://superset.example.com"
README = Path(__file__).resolve().parent.parent / "README.md"
README_RU = Path(__file__).resolve().parent.parent / "README_RU.md"
MIN_DESCRIPTION_CHARS = 10  # "Rename a role." is terse but fine; an empty or one-word description is not


@pytest.fixture
def mcp_server(monkeypatch):
    client = SupersetClient(auth_manager=CookieAuthManager(base_url=BASE, cookie_value="c"), base_url=BASE)
    monkeypatch.setattr(server_module, "superset_client", client)
    mcp = FastMCP(name="test")
    register_all_tools(mcp)
    return mcp


def _hint(annotations, camel):
    """Read an MCP annotation across SDK versions (fastmcp 3.x: readOnlyHint, 4.x: read_only_hint)."""
    snake = "".join("_" + ch.lower() if ch.isupper() else ch for ch in camel)
    return getattr(annotations, snake) if hasattr(annotations, snake) else getattr(annotations, camel, None)


def _input_schema(tool) -> dict:
    schema = tool.input_schema if hasattr(tool, "input_schema") else tool.inputSchema
    return schema or {}


@pytest.fixture
async def tools(mcp_server):
    async with Client(mcp_server) as c:
        return await c.list_tools()


async def test_tool_names_are_unique_valid_and_prefixed(tools):
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), f"duplicate tool names: {sorted({n for n in names if names.count(n) > 1})}"
    bad = [n for n in names if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", n)]
    assert not bad, f"names outside the MCP charset: {bad}"
    unprefixed = [n for n in names if not n.startswith("superset_")]
    assert not unprefixed, f"tools without the 'superset_' prefix would collide in a multi-server client: {unprefixed}"


async def test_every_tool_has_a_real_description(tools):
    short = {
        t.name: (t.description or "").strip()
        for t in tools
        if len((t.description or "").strip()) < MIN_DESCRIPTION_CHARS
    }
    assert not short, f"missing or too-short descriptions: {short}"


async def test_every_tool_takes_an_object_of_named_parameters(tools):
    bad = [t.name for t in tools if _input_schema(t).get("type") != "object"]
    assert not bad, f"inputSchema root must be an object: {bad}"


async def test_every_tool_declares_consistent_safety_hints(tools):
    # MCP: destructiveHint is only meaningful for tools that are not read-only, so a read-only tool
    # legitimately leaves it unset; every writing tool must say whether it is destructive.
    missing, contradictory = [], []
    for t in tools:
        read_only = _hint(t.annotations, "readOnlyHint") if t.annotations else None
        destructive = _hint(t.annotations, "destructiveHint") if t.annotations else None
        if read_only is None or (read_only is False and destructive is None):
            missing.append(t.name)
        elif read_only and destructive:
            contradictory.append(t.name)
    assert not missing, f"tools without a complete readOnlyHint/destructiveHint: {missing}"
    assert not contradictory, f"tools marked both read-only and destructive: {contradictory}"


async def test_readme_tool_count_matches_the_registered_catalogue(tools):
    text = README.read_text(encoding="utf-8")
    heading = re.search(r"^## Available Tools \((\d+)\)", text, re.MULTILINE)
    total_row = re.search(r"\*\*Total tools\*\* \| \*\*(\d+)\*\*", text)
    assert heading and total_row, "README no longer has the 'Available Tools (N)' heading or the 'Total tools' row"
    assert int(heading.group(1)) == len(tools), f"README heading says {heading.group(1)}, server registers {len(tools)}"
    assert int(total_row.group(1)) == len(tools), (
        f"README table says {total_row.group(1)}, server registers {len(tools)}"
    )


async def test_russian_readme_tool_count_matches_the_registered_catalogue(tools):
    text = README_RU.read_text(encoding="utf-8")
    heading = re.search(r"^## Доступные инструменты \((\d+)\)", text, re.MULTILINE)
    total_row = re.search(r"\*\*Всего инструментов\*\* \| \*\*(\d+)\*\*", text)
    assert heading and total_row, "README_RU no longer has the tool-count heading or the total row"
    assert int(heading.group(1)) == len(tools), (
        f"README_RU heading says {heading.group(1)}, server registers {len(tools)}"
    )
    assert int(total_row.group(1)) == len(tools), (
        f"README_RU table says {total_row.group(1)}, server registers {len(tools)}"
    )

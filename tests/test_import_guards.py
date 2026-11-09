"""Import tools and the overwrite guard.

``overwrite=True`` replaces existing dashboards/charts/datasets with the content of the ZIP, which can
roll an instance back. ``superset_assets_import`` already demanded ``confirm_overwrite=True``; the
three single-type imports did not. They now share one contract:

* ``overwrite`` left False  -> upload with overwrite=false, no confirmation needed;
* ``overwrite=True`` alone   -> REJECTED, nothing is uploaded;
* ``overwrite=True`` + ``confirm_overwrite=True`` -> upload with overwrite=true.

``superset_dashboard_embedded_delete`` likewise gets the ``confirm_delete`` every other delete tool has.
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

# tool -> (import endpoint, multipart field name the endpoint expects)
IMPORTS = {
    "superset_dashboard_import": ("/dashboard/import/", "formData"),
    "superset_chart_import": ("/chart/import/", "formData"),
    "superset_dataset_import": ("/dataset/import/", "formData"),
    "superset_assets_import": ("/assets/import/", "bundle"),
}


@pytest.fixture
def mcp_server(monkeypatch):
    client = SupersetClient(auth_manager=CookieAuthManager(base_url=BASE, cookie_value="c"), base_url=BASE)
    monkeypatch.setattr(server_module, "superset_client", client)
    mcp = FastMCP(name="test")
    register_all_tools(mcp)
    return mcp


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "export.zip"
    path.write_bytes(b"PK\x03\x04 not really a zip, the server is mocked")
    return str(path).replace("\\", "/")


def _text(result):
    content = getattr(result, "content", result)
    if isinstance(content, list) and content:
        return content[0].text
    return str(content)


async def _call(mcp_server, tool, args):
    async with Client(mcp_server) as c:
        return json.loads(_text(await c.call_tool(tool, args)))


def _mock(endpoint):
    respx.get(f"{API}/security/csrf_token/").mock(return_value=httpx.Response(200, json={"result": "csrf"}))
    return respx.post(f"{API}{endpoint}").mock(return_value=httpx.Response(200, json={"message": "OK"}))


def _form_overwrite(route) -> str:
    body = route.calls.last.request.content.decode("utf-8", errors="replace")
    marker = 'name="overwrite"'
    assert marker in body, body[:300]
    return body.split(marker, 1)[1].split("\r\n\r\n", 1)[1].split("\r\n", 1)[0]


@respx.mock
@pytest.mark.parametrize("tool", IMPORTS)
async def test_import_without_overwrite_uploads_the_file_and_asks_for_nothing(mcp_server, bundle, tool):
    endpoint, field = IMPORTS[tool]
    route = _mock(endpoint)

    out = await _call(mcp_server, tool, {"file_path": bundle})

    assert out == {"message": "OK"}
    assert f'name="{field}"' in route.calls.last.request.content.decode("utf-8", errors="replace")
    assert _form_overwrite(route) == "false"


@respx.mock
@pytest.mark.parametrize("tool", IMPORTS)
async def test_overwrite_without_confirmation_is_rejected_and_nothing_is_uploaded(mcp_server, bundle, tool):
    endpoint, _ = IMPORTS[tool]
    route = _mock(endpoint)

    out = await _call(mcp_server, tool, {"file_path": bundle, "overwrite": True})

    assert out["error"].startswith("REJECTED")
    assert "confirm_overwrite=True" in out["error"]
    assert not route.called


@respx.mock
@pytest.mark.parametrize("tool", IMPORTS)
async def test_confirmation_alone_does_not_turn_overwrite_on(mcp_server, bundle, tool):
    """confirm_overwrite confirms an overwrite; it must not imply one."""
    endpoint, _ = IMPORTS[tool]
    route = _mock(endpoint)

    await _call(mcp_server, tool, {"file_path": bundle, "confirm_overwrite": True})

    assert _form_overwrite(route) == "false"


@respx.mock
@pytest.mark.parametrize("tool", IMPORTS)
async def test_confirmed_overwrite_is_sent_as_overwrite_true(mcp_server, bundle, tool):
    endpoint, _ = IMPORTS[tool]
    route = _mock(endpoint)

    out = await _call(mcp_server, tool, {"file_path": bundle, "overwrite": True, "confirm_overwrite": True})

    assert out == {"message": "OK"}
    assert _form_overwrite(route) == "true"


@respx.mock
async def test_the_rejection_names_what_an_overwrite_replaces(mcp_server, bundle):
    _mock("/dashboard/import/")

    out = await _call(mcp_server, "superset_dashboard_import", {"file_path": bundle, "overwrite": True})

    assert "dashboard" in out["error"].lower()


# ------------------------------------------------------------ embedded_delete


@respx.mock
async def test_embedded_delete_needs_confirmation(mcp_server):
    respx.get(f"{API}/security/csrf_token/").mock(return_value=httpx.Response(200, json={"result": "csrf"}))
    delete = respx.delete(f"{API}/dashboard/5/embedded").mock(return_value=httpx.Response(200, json={"message": "OK"}))

    out = await _call(mcp_server, "superset_dashboard_embedded_delete", {"dashboard_id": 5})
    assert out["error"].startswith("REJECTED") and "confirm_delete=True" in out["error"]
    assert "iframe" in out["error"]  # says what stops working
    assert not delete.called

    out = await _call(mcp_server, "superset_dashboard_embedded_delete", {"dashboard_id": 5, "confirm_delete": True})
    assert out == {"message": "OK"} and delete.call_count == 1

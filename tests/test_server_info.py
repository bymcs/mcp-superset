"""What a client learns about the server during the MCP handshake."""

from fastmcp import Client

import mcp_superset.server as server_module
from mcp_superset import __version__


def _server_info(client):
    """fastmcp 4.x exposes ``Client.server_info``; 3.x only has ``initialize_result.serverInfo``."""
    info = getattr(client, "server_info", None)
    return info if info is not None else client.initialize_result.serverInfo


async def test_server_reports_the_package_version_in_initialize():
    """Without this the handshake reports *fastmcp's* version, so a client or proxy cannot tell
    a mcp-superset release from a patched working tree."""
    async with Client(server_module.mcp) as c:
        info = _server_info(c)

    assert info.name == "superset"
    assert info.version == __version__

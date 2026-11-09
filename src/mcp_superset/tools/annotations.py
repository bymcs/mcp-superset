"""MCP tool annotations (readOnlyHint / destructiveHint), derived from tool names.

None of the tools used to carry annotations, so MCP clients and proxies could not tell
``superset_chart_list`` from ``superset_user_delete`` and treated every tool alike. The
hints are advisory (the confirm_* flags remain the actual safety net), but they let a
client route calls by risk.

Classification is by tool name so a new tool is covered automatically. The rules are
deliberately conservative: anything not clearly read-only is reported as a write, and
anything that deletes, replaces or overwrites is reported as destructive.
"""

from typing import Any

# Tokens (the "_"-separated words of a tool name) that make a tool destructive.
_DESTRUCTIVE_TOKENS = {"delete", "remove", "revoke", "reset", "replace", "stop", "import"}

# Destructive tools whose names carry no destructive token.
_DESTRUCTIVE_NAMES = {
    "superset_role_permission_add",  # replaces the role's ENTIRE permission set
    "superset_role_copy_permissions",  # overwrites the target role's permissions
}

# Tokens that make a tool read-only.
_READ_ONLY_TOKENS = {"list", "get", "export"}

# Read-only tools whose names carry none of the read-only tokens.
_READ_ONLY_NAMES = {
    "superset_chart_data",  # runs a query, changes nothing
    "superset_dashboard_charts",
    "superset_dashboard_datasets",
    "superset_database_available_engines",
    "superset_database_catalogs",
    "superset_database_connection_info",
    "superset_database_function_names",
    "superset_database_related_objects",
    "superset_database_schemas",
    "superset_database_select_star",
    "superset_database_table_metadata",
    "superset_database_tables",
    "superset_database_test_connection",
    "superset_database_validate_parameters",
    "superset_database_validate_sql",
    "superset_dataset_related_objects",
    "superset_permissions_audit",
    "superset_recent_activity",
    "superset_sqllab_estimate_cost",
    "superset_sqllab_format_sql",
    "superset_sqllab_results",
}


def annotations_for(tool_name: str) -> dict[str, Any]:
    """Return the MCP annotations for a tool, based on its name."""
    tokens = set(tool_name.split("_"))
    # openWorldHint: every tool talks to an external Superset instance.
    if tool_name in _DESTRUCTIVE_NAMES or tokens & _DESTRUCTIVE_TOKENS:
        return {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}
    if tool_name in _READ_ONLY_NAMES or tokens & _READ_ONLY_TOKENS:
        return {"readOnlyHint": True, "openWorldHint": True}
    return {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}


def install_annotations(mcp) -> None:
    """Make every ``@mcp.tool`` registered from now on carry name-derived annotations.

    Wraps ``mcp.tool`` on the instance; call ``uninstall_annotations`` when done.
    Tools that pass their own ``annotations=`` are left alone.
    """
    original = mcp.tool

    def tool(name_or_fn=None, **kwargs):
        if callable(name_or_fn) and "annotations" not in kwargs:
            kwargs["annotations"] = annotations_for(name_or_fn.__name__)
        return original(name_or_fn, **kwargs)

    mcp.tool = tool


def uninstall_annotations(mcp) -> None:
    """Remove the wrapper installed by ``install_annotations``."""
    mcp.__dict__.pop("tool", None)

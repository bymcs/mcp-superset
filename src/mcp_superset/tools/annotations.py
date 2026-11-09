"""MCP tool annotations (readOnlyHint / destructiveHint), derived from tool names.

None of the tools used to carry annotations, so MCP clients and proxies could not tell
``superset_chart_list`` from ``superset_user_delete`` and treated every tool alike. The
hints are advisory (the confirm_* flags remain the actual safety net), but they let a
client route calls by risk.

Classification is by tool name so a new tool is covered automatically. The rules are
deliberately conservative: anything not clearly read-only is reported as a write, and
anything that deletes, replaces or overwrites is reported as destructive.
"""

import os
from typing import Any

# Profiles: "full" registers every tool; "readonly" registers only tools that cannot change anything.
PROFILES = ("full", "readonly")
PROFILE_ENV = "SUPERSET_MCP_PROFILE"

# Tokens that make a tool a writer even when its name also contains a read-only token
# (superset_dataset_get_or_create reads in its name but CREATES a dataset when it is missing).
_WRITE_TOKENS = {"create", "update", "add", "set", "copy", "duplicate", "execute", "refresh", "publish", "grant"}

# Read-only tools that the "readonly" profile still leaves out: they take a connection URI from the caller and
# make the Superset server open an outbound connection to it, which is not something to offer a session that
# only needs to look.
_READONLY_PROFILE_EXCLUDED = {
    "superset_database_test_connection",
    "superset_database_validate_parameters",
}

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
    if tool_name in _READ_ONLY_NAMES or (tokens & _READ_ONLY_TOKENS and not tokens & _WRITE_TOKENS):
        return {"readOnlyHint": True, "openWorldHint": True}
    return {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}


def resolve_profile(value: str | None = None) -> str:
    """Return the active profile: ``value`` if given, else ``$SUPERSET_MCP_PROFILE``, else ``"full"``.

    An unknown value raises instead of falling back: a typo must never leave someone believing the
    writing tools are switched off while they are on.
    """
    raw = value if value is not None else os.environ.get(PROFILE_ENV, "")
    profile = raw.strip().lower() or "full"
    if profile not in PROFILES:
        raise ValueError(f"{PROFILE_ENV}={raw!r} is not a known profile; use one of: {', '.join(PROFILES)}")
    return profile


def allowed_in_profile(tool_name: str, profile: str) -> bool:
    """Whether a tool is registered under ``profile``."""
    if profile == "full":
        return True
    return annotations_for(tool_name)["readOnlyHint"] and tool_name not in _READONLY_PROFILE_EXCLUDED


def install_annotations(mcp, profile: str = "full") -> None:
    """Make every ``@mcp.tool`` registered from now on carry name-derived annotations.

    Wraps ``mcp.tool`` on the instance; call ``uninstall_annotations`` when done. Tools that pass their
    own ``annotations=`` are left alone. Under a restricted ``profile`` a tool that is not allowed is not
    registered at all (the function is returned undecorated), so it can be neither listed nor called.
    """
    original = mcp.tool

    def tool(name_or_fn=None, **kwargs):
        if callable(name_or_fn):
            if not allowed_in_profile(name_or_fn.__name__, profile):
                return name_or_fn
            if "annotations" not in kwargs:
                kwargs["annotations"] = annotations_for(name_or_fn.__name__)
        return original(name_or_fn, **kwargs)

    mcp.tool = tool


def uninstall_annotations(mcp) -> None:
    """Remove the wrapper installed by ``install_annotations``."""
    mcp.__dict__.pop("tool", None)

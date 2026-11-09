# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`superset_dataset_column_add`** — add or update **one** dataset column without rebuilding the whole list. Superset's `PUT /dataset/{id}` replaces *all* columns with the list it receives, so changing a single column through `superset_dataset_update(columns=...)` meant passing every column with its id by hand. The new tool reads the dataset, upserts the one column by name, and writes every column back with its id, so existing verbose names, descriptions and flags survive; `always_filter_main_dttm` is preserved as well. Useful for exposing a column a dataset's SQL now returns (including datasets whose Jinja `filter_values` makes `superset_dataset_refresh_schema` unusable without a dashboard filter), for calculated columns (`expression`), and for setting a `verbose_name`. It is additive, so it does not need `confirm_columns_replace`.
- **Tool annotations** — every tool now declares `readOnlyHint` / `destructiveHint` (and `openWorldHint`), derived from the tool name (`tools/annotations.py`). Previously none did, so MCP clients and proxies could not tell `superset_chart_list` from `superset_user_delete` and treated every tool as a read. Lists, gets and exports are read-only; deletes, removals, revocations, resets, replacements, imports, `superset_query_stop`, `superset_role_permission_add` and `superset_role_copy_permissions` are destructive; everything else is a write. The hints are advisory; the `confirm_*` flags remain the actual safeguard.

### Changed

- **Minimum `fastmcp` is now 3.2.4** (was 3.1.0), and the lockfile pins 3.2.4. FastMCP turns each tool docstring's `Args:` section into per-parameter schema descriptions only from 3.2.4 on; with 3.1.0 all 410 parameters of the 138 tools were published without a description, so an agent saw bare names such as `q` or `page_size`. Against 3.2.4 and 4.1.6 every parameter now carries its description. A new test (`tests/test_tool_schemas.py`) keeps this from regressing. If you pin an older fastmcp, upgrade it.

- **The MCP handshake now reports mcp-superset's own version.** `FastMCP` was created without `version`, so `serverInfo.version` carried *fastmcp's* version (e.g. `3.2.4`), which made a mcp-superset release indistinguishable from a patched working tree for clients and proxies. It now equals `mcp_superset.__version__`, like `GET /health` and `--version` already did.

- **BREAKING (guard): `superset_dashboard_import`, `superset_chart_import` and `superset_dataset_import` now require `confirm_overwrite=True` together with `overwrite=True`**, exactly as `superset_assets_import` already did. `overwrite=True` replaces every existing object sharing a UUID with the ZIP's content and cannot be undone, yet three of the four import tools did it without any confirmation. Without the new flag the call returns a `REJECTED:` error and uploads nothing; imports with `overwrite` left `False` are unchanged. Callers that pass `overwrite=True` must add `confirm_overwrite=True`.
- **BREAKING (guard): `superset_dashboard_embedded_delete` now requires `confirm_delete=True`**, like every other delete tool; it was the only one without a confirmation, and disabling embedding breaks every iframe that uses it.

### Fixed

- **`superset_user_delete` and `superset_role_delete` guards failed open when the lookup they depend on failed.** `superset_user_delete` skipped its "never delete the service account" check if `GET /api/v1/me/` errored (`except Exception: pass`), and `superset_role_delete` fell back to the name `ID=<n>` when the role could not be read, which matches no protected role, so a confirmed delete of `Admin` would go through. Both now fail closed: if the server cannot establish who it is logged in as (error, or a response without `pk`/`id`), or cannot read the role's name, the call returns a `BLOCKED:` error that says why and deletes nothing. A legitimate delete is unchanged (verified against a live Superset 6.1, whose `/me/` returns `id`).
- **SQL Lab DDL/DML guard in `superset_sqllab_execute` could be bypassed with a comment marker inside a quoted region.** The guard stripped comments *before* string literals, so `SELECT '--'; DROP TABLE t` or `SELECT '/*'; DROP TABLE t; SELECT '*/'` looked like `SELECT '` and passed. The same hole existed for `"…"`, `` `…` ``, `[…]` and `$$…$$` quoting, for MySQL (`5--1; DROP …`, where `--` is a comment only before whitespace, and `/*!… */` conditional comments, which MySQL executes), and for a `-- ` inside a `/*! … */` region. Comments and literals are now resolved together in a single left-to-right pass, and wherever engines disagree the scanner errs towards seeing *more* code (backslash is not treated as an escape, anything unterminated is scanned as code). Keywords inside properly closed single-quoted literals are no longer reported (they were false positives whenever the literal contained a comment marker). Remember this guard is a best-effort safety net; the authoritative protection is `allow_dml=false` on the database connection.
- **`superset_dataset_update` reset `always_filter_main_dttm` to `false`** whenever `columns` was not part of the update. Superset resets the flag on any `PUT` that omits it, but only a column replacement read and preserved the current value, so a `sql`-only, `description`-only, `metrics`-only or `table_name`-only update silently broke the time filters of dependent dashboards. The current value is now preserved on every update; passing `always_filter_main_dttm` explicitly still wins.
- **`superset_chart_update` / `superset_chart_create` demanded `granularity_sqla` even when the chart's dataset has no temporal column at all**, so such charts could never be updated through the server (`null` was rejected too). The requirement now applies only when the dataset has a temporal column (`main_dttm_col` set or a column with `is_dttm`). When the dataset cannot be read, or the chart's `viz_type` is unknown, the previous strict behaviour is kept.

### Documentation

- `superset_dashboard_filter_reset`: the JSON example of a filter definition was listed under `confirm_reset` instead of `filters_json`, so the published schema described `filters_json` as "…Each element:" (cut off) and `confirm_reset`, a boolean, as a multi-line JSON blob. Visible only since the `Args:` section became schema descriptions (fastmcp ≥ 3.2.4). New registry tests fail on any parameter description that ends with `:` or on a boolean whose description carries a `{`.
- `superset_dashboard_filter_update`: `cascade_parent_id` was documented as "None — remove cascading", but omitting it (`None`) leaves the cascade unchanged; only an empty string (`""`) removes it. The description now says so, and a test keeps the text and the behaviour in step.
- `superset_chart_data`: filters for Jinja `filter_values('COL')` datasets must be passed as `adhoc_filters` **inside `queries[0]`** — Superset reads them from the first query, not from a top-level `form_data` key. The tool description now says so and shows the shape; the response's `applied_template_filters` lists the columns that reached the template.

## [0.3.1] - 2026-07-25

### Fixed

- **Native filter tools crashed on dashboards whose `json_metadata` is null** (#11). Superset returns `json_metadata: null` (not `"{}"`) for dashboards that have never had metadata written, so `superset_dashboard_filter_add/list/update/delete/reset` raised `TypeError: the JSON object must be str, bytes or bytearray, not NoneType` before making any API call. Null `json_metadata` and `position_json` are now read as empty objects everywhere.
- **List arguments were unusable when the client sent them JSON-encoded** (#12). Some MCP clients serialise `dashboards=[31]` as the string `"[31]"`, which pydantic rejected with `list_type`, so `superset_chart_update(dashboards=...)` never ran. All list parameters (`dashboards`, `roles`, `owners`, `users`, `user_ids`, `role_ids`, `tables`, `permission_view_menu_ids`, `allowed_domains`) now accept a native list, a JSON-encoded list, a bare value, or a comma-separated string.

### Changed

- Superset error responses of the form `{"errors": [{"message": ..., "error_type": ..., "extra": {"issue_codes": [...]}}]}` are now unpacked into the raised error instead of being stringified, so a 500 carries the error type and issue codes rather than a bare "Fatal error".

## [0.3.0] - 2026-07-25

### Added

- **Session-cookie authentication for SSO/OAuth instances.** Where Superset sits behind OAuth/OIDC/SAML, the REST login endpoint is unusable. Setting `SUPERSET_SESSION_COOKIE` (and `SUPERSET_SESSION_COOKIE_NAME` when the instance renames the cookie) makes the server send a browser session cookie on every request and fetch CSRF tokens with it. Cookie mode takes precedence over `SUPERSET_USERNAME`/`SUPERSET_PASSWORD`; a 401 in this mode says the cookie needs refreshing, since an expired SSO session cannot be renewed server-side.
- Test suite (`pytest` + `respx`), covering both auth strategies, the client wiring and the 401 paths. CI now runs it.

### Changed

- Authentication is now an `AuthStrategy` protocol with two implementations (`JwtAuthManager`, `CookieAuthManager`) selected by `build_auth_strategy()`. `SupersetClient` no longer knows about bearer tokens. `AuthManager` was renamed to `JwtAuthManager`.

### Fixed

- **Authentication failures on mutating requests escaped as raw `httpx.HTTPStatusError`.** A rejected session or a failed login surfaced as an httpx exception from the CSRF/login call, bypassing `SupersetAPIError` and the "refresh your cookie" hint. All auth and CSRF failures are now wrapped consistently.
- `post_form` built its headers by hand (a third copy of the auth/CSRF logic) and so missed the same error handling; it now shares `_get_headers`. Error-detail extraction is a single helper used by `_request`, `get_raw` and `post_form`.

## [0.2.8] - 2026-07-13

### Fixed

- **`superset_dataset_update` silently reset `always_filter_main_dttm` to false.** Superset's `PUT /dataset/{id}` drops the flag whenever it is omitted from the payload, so updating a dataset's `columns` (e.g. to set Russian `verbose_name` labels) turned off the main datetime filter and broke time filtering on every dependent dashboard. The tool now preserves the current value automatically when `columns` are replaced, and exposes a new optional `always_filter_main_dttm` parameter to set it explicitly.

## [0.2.7] - 2026-06-15

### Fixed

- **`--version` reported a stale, hard-coded version.** The CLI banner was pinned to `0.2.4` in `__main__.py`, so `mcp-superset --version` printed the wrong version regardless of the installed package. It now derives the version from `mcp_superset.__version__`, keeping the CLI in sync with the package version automatically.

## [0.2.6] - 2026-06-09

### Fixed

- **Bulk role operations were broken** — `superset_bulk_user_role_add`, `superset_bulk_user_role_remove`, `superset_bulk_user_role_replace` and `superset_role_copy_permissions` passed `json=` instead of `json_data=` to the client, raising `TypeError` at apply time (dry-run was unaffected, so it went unnoticed).
- **DDL/DML guard in `superset_sqllab_execute` could be bypassed** — the check only inspected the first keyword, so `WITH ... DELETE`, chained `SELECT 1; DROP ...`, parenthesised `(DELETE ...)`, `EXPLAIN ... DELETE`, `COPY` and `DO $$ BEGIN EXECUTE '...' END $$` (DDL hidden in a string literal) slipped through. It now matches dangerous keywords (including `DO`/`EXECUTE`) as whole words anywhere in the query, after stripping comments and string literals, and names the most specific operation in the rejection message.
- **List pagination ignored `page`/`page_size`** — most `*_list` tools (including `superset_group_list`) sent them as plain query params, which Superset ignores (always returning the first page). They now use RISON pagination via the new `client.get_page()` helper. Custom endpoints (`recent_activity`, `tag/get_objects`) keep query params, as they genuinely read them.
- **CSRF token was never refreshed on expiry** — a stale CSRF (which has its own, shorter lifetime than the JWT) caused a 400 with no retry. The client now detects CSRF-related 400s and retries once with a fresh token, without masking genuine validation errors.
- **`granularity_sqla` guard was too strict** — it blocked legitimate non-temporal charts (maps, pie, word cloud, hierarchical). It is now required only for viz types that actually have a temporal axis.
- **Unhandled `JSONDecodeError` on malformed JSON arguments** — tools accepting JSON strings (`columns`, `metrics`, `recipients`, `query_context`, `objects_to_tag`, `tags`, `position_json`, `filters_json`) now return a structured `{"error": ...}` instead of crashing.
- **Auto-sync of `datasource_access`** used `PUT .../permissions/` (trailing slash) instead of the working `POST .../permissions`, so access grants on dashboard/chart create/update silently failed.

### Changed

- Removed reference to a non-existent `superset_rls_create_unsafe` tool in the Base RLS rejection message.
- Consolidated three near-duplicate `datasource_access` permission lookups into a single `find_datasource_permissions` helper.
- Standardised the dataset `related_objects` endpoint to a trailing slash, matching the database endpoint.

## [0.2.5] - 2026-04-05

### Added

- **Bulk role operations** (4 new tools):
  - `superset_bulk_user_role_add` — add a role to multiple users (by user IDs or by current role filter)
  - `superset_bulk_user_role_remove` — remove a role from multiple users (prevents removing last role)
  - `superset_bulk_user_role_replace` — replace one role with another for all users who have it
  - `superset_role_copy_permissions` — copy all permissions from one role to another
  - All bulk operations support dry-run mode and exclude Admin users by default
- **Improved permissions audit** (`superset_permissions_audit`):
  - Now checks both dashboard visibility (via `dashboard.roles`) AND `datasource_access`
  - Three access states: `1` (full access), `0` (no access), `"visible_no_data"` (can open dashboard but charts fail)
  - Previously only checked `datasource_access`, missing role-based dashboard visibility

### Changed

- Total tools count: 119 (was 115)

## [0.2.4] - 2026-03-11

### Added

- Extended README badges: PyPI downloads, CodeQL, Superset version, MCP compatible, py.typed, Ruff, uv, tools count, GitHub stars
- Official MCP Registry support (`server.json`, `mcp-name` verification tag)
- Glama.ai listing: https://glama.ai/mcp/servers/bintocher/mcp-superset
- Smithery configuration (`smithery.yaml`)

## [0.2.2] - 2025-03-11

### Changed

- Updated GitHub Actions to latest versions via Dependabot:
  - `actions/upload-artifact` v6 → v7
  - `actions/download-artifact` v6 → v8
  - `github/codeql-action` v3 → v4

## [0.2.1] - 2025-03-11

### Added

- Health check endpoint: `GET /health` returns server status, version, and Superset URL (no auth required)
- PEP 561 `py.typed` marker for typed package support
- CONTRIBUTING.md with development setup and contribution guidelines
- SECURITY.md with responsible disclosure policy
- GitHub issue templates (bug report, feature request) and PR template
- Dependabot configuration for automated dependency updates (pip + GitHub Actions)
- Pre-commit hooks configuration (ruff, trailing whitespace, YAML check)
- CodeQL security scanning workflow

### Changed

- All comments, docstrings, and error messages translated to English
- Google-style docstrings added to all public functions and methods

## [0.2.0] - 2025-03-11

### Changed

- Renamed Python package from `superset_mcp` to `mcp_superset` for consistency with PyPI name `mcp-superset`
- Import is now `import mcp_superset` (was `import superset_mcp`)
- CLI entry point: `python -m mcp_superset` (was `python -m superset_mcp`)

## [0.1.0] - 2025-03-10

### Added

- Initial release
- 128+ MCP tools covering complete Apache Superset 6.0.1 REST API
- Dashboard management: CRUD, copy, publish/unpublish, export/import, embedded mode
- Chart management: CRUD, copy, data retrieval, export/import, cache warmup
- Database management: CRUD, connection testing, schema/table introspection
- Dataset management: CRUD, duplicate, schema refresh, export/import
- SQL Lab: query execution, formatting, results retrieval, cost estimation
- Saved queries: full CRUD
- Security: user/role management, permissions, RLS (Row Level Security)
- Group management with role/user assignment
- Dashboard native filters: add, update, delete, reset
- Tag management with object binding
- Report scheduling and annotation layers
- Asset export/import (full instance backup/restore)
- Audit tool: comprehensive permissions matrix
- JWT authentication with automatic token refresh
- CSRF token handling for state-changing operations
- Built-in safety validations and confirmation flags for destructive operations
- Automatic datasource_access synchronization
- DDL/DML blocking in SQL Lab
- Streamable HTTP transport (stateless mode)
- CLI with configurable host/port
- Environment variable and `.env` file configuration

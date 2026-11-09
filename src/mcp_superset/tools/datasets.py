"""Tools for managing Superset datasets."""

import base64
import json

from mcp_superset.tools.helpers import parse_json_arg

# Dataset column fields Superset accepts on PUT /dataset/{id} (read-only fields such as uuid,
# changed_on, type_generic are deliberately absent).
_WRITABLE_COLUMN_FIELDS = (
    "id",
    "column_name",
    "verbose_name",
    "description",
    "expression",
    "filterable",
    "groupby",
    "is_active",
    "is_dttm",
    "python_date_format",
    "type",
    "advanced_data_type",
    "extra",
)


def register_dataset_tools(mcp):
    from mcp_superset.server import superset_client as client

    @mcp.tool
    async def superset_dataset_list(
        page: int = 0,
        page_size: int = 25,
        q: str | None = None,
        get_all: bool = False,
    ) -> str:
        """List Superset datasets with pagination.

        A dataset is a reference to a table/view or a virtual SQL query in Superset.
        IMPORTANT: always call before dataset_get to discover current IDs.

        Args:
            page: Page number (starting from 0).
            page_size: Number of records per page (max 100).
            q: RISON filter for searching. Examples:
                - By name: (filters:!((col:table_name,opr:ct,value:search_term)))
                - By schema: (filters:!((col:schema,opr:eq,value:public)))
                - By database: (filters:!((col:database,opr:rel_o_m,value:1)))
            get_all: Fetch ALL records with automatic pagination (ignores page/page_size).

        Returns:
            JSON string with the list of datasets.
        """
        if get_all:
            params = {}
            if q:
                params["q"] = q
            result = await client.get_all("/api/v1/dataset/", params=params)
        else:
            result = await client.get_page("/api/v1/dataset/", page, page_size, q)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_dataset_get(dataset_id: int) -> str:
        """Get detailed information about a dataset: columns, metrics, SQL.

        IMPORTANT: if the ID is unknown, call superset_dataset_list first.

        Args:
            dataset_id: Dataset ID (integer from dataset_list result).

        Returns:
            JSON string with dataset details.
        """
        result = await client.get(f"/api/v1/dataset/{dataset_id}")
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_dataset_create(
        table_name: str,
        database: int,
        schema_name: str | None = None,
        sql: str | None = None,
    ) -> str:
        """Create a new dataset (physical or virtual).

        A physical dataset references an existing table/view in the database.
        A virtual dataset uses an arbitrary SQL query as the data source.

        Args:
            table_name: Table/view name (for physical) or dataset name (for virtual).
            database: Database connection ID (from superset_database_list).
            schema_name: Database schema (e.g. "public", "source"). If omitted, uses the DB default schema.
            sql: SQL query for a virtual dataset. If provided, creates a virtual dataset based on this query.

        Returns:
            JSON string with the created dataset details.
        """
        payload = {"table_name": table_name, "database": database}
        if schema_name is not None:
            payload["schema"] = schema_name
        if sql is not None:
            payload["sql"] = sql
        result = await client.post("/api/v1/dataset/", json_data=payload)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_dataset_update(
        dataset_id: int,
        table_name: str | None = None,
        sql: str | None = None,
        description: str | None = None,
        columns: str | None = None,
        metrics: str | None = None,
        confirm_columns_replace: bool = False,
        always_filter_main_dttm: bool | None = None,
    ) -> str:
        """Update a dataset. Only pass the fields you want to change.

        Args:
            dataset_id: ID of the dataset to update.
            table_name: New table/dataset name.
            sql: New SQL query (only for virtual datasets).
            description: Dataset description (displayed in Superset UI).
            columns: JSON string describing columns.
                CRITICAL: passing columns REPLACES ALL dataset columns!
                To update a single column (e.g. verbose_name), pass ALL columns with their IDs.
                After an error, dataset_refresh_schema will restore columns from SQL.
                Format: [{"id": 123, "column_name": "id", "type": "INTEGER", ...}]
            metrics: JSON string describing metrics. Format:
                [{"metric_name": "count", "expression": "COUNT(*)", "metric_type": "count"}]
            confirm_columns_replace: Confirmation for replacing ALL columns (REQUIRED when passing columns).
            always_filter_main_dttm: Force the dataset's main datetime filter on/off.
                Superset resets this flag to false on any PUT that omits it, which
                breaks dashboard time filters. Pass an explicit value to set it;
                leave it None and the current value is preserved automatically
                on every update (sql, columns, metrics, description, ...).

        Returns:
            JSON string with the updated dataset details.
        """
        # Guard against accidental column loss
        if columns is not None and not confirm_columns_replace:
            return json.dumps(
                {
                    "error": (
                        "REJECTED: columns provided without confirm_columns_replace=True. "
                        "Superset PUT /dataset/{id} with the columns field REPLACES ALL dataset "
                        "columns with the provided list. To update a single column "
                        "(e.g. verbose_name), pass ALL columns with their IDs. "
                        "First retrieve current columns via dataset_get, "
                        "then pass the full list with confirm_columns_replace=True. "
                        "On error, dataset_refresh_schema will restore columns from SQL."
                    )
                },
                ensure_ascii=False,
            )

        payload = {}
        if table_name is not None:
            payload["table_name"] = table_name
        if sql is not None:
            payload["sql"] = sql
        if description is not None:
            payload["description"] = description
        if columns is not None:
            parsed, err = parse_json_arg(columns, "columns")
            if err:
                return json.dumps({"error": err}, ensure_ascii=False)
            payload["columns"] = parsed
        if metrics is not None:
            parsed, err = parse_json_arg(metrics, "metrics")
            if err:
                return json.dumps({"error": err}, ensure_ascii=False)
            payload["metrics"] = parsed
        # Superset PUT /dataset/{id} resets always_filter_main_dttm to false when
        # the field is omitted from the payload — for ANY update, not only when
        # columns are replaced (a sql-only, description-only or metrics-only
        # update clobbers it too). That silently breaks the time filters on
        # dependent dashboards. Use the explicit value when provided; otherwise
        # read and preserve the current value on every update.
        if always_filter_main_dttm is not None:
            payload["always_filter_main_dttm"] = always_filter_main_dttm
        else:
            current = await client.get(f"/api/v1/dataset/{dataset_id}")
            cur_val = (current.get("result") or {}).get("always_filter_main_dttm")
            if cur_val is not None:
                payload["always_filter_main_dttm"] = cur_val
        result = await client.put(f"/api/v1/dataset/{dataset_id}", json_data=payload)
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_dataset_column_add(
        dataset_id: int,
        column_name: str,
        verbose_name: str | None = None,
        description: str | None = None,
        expression: str | None = None,
        type: str | None = None,
        is_dttm: bool | None = None,
        filterable: bool | None = None,
        groupby: bool | None = None,
        is_active: bool | None = None,
    ) -> str:
        """Add ONE column to a dataset, or update ONE existing column, leaving all others untouched.

        Superset's PUT /dataset/{id} replaces ALL columns with the list it receives, so changing a
        single column through dataset_update(columns=...) means rebuilding the whole list by hand.
        This tool does that round trip for you: it reads the dataset, upserts the one column by
        name, and writes every column back with its id, so existing verbose names, descriptions
        and flags survive. It also preserves always_filter_main_dttm.

        Typical uses: expose a column your dataset SQL now returns (keeping the other columns'
        metadata), add a calculated column (expression), or set a column's verbose_name.
        If the dataset SQL needs a Jinja filter and dataset_refresh_schema cannot run it
        without a dashboard filter value, this is the way to register the new column.

        Args:
            dataset_id: Dataset ID.
            column_name: Column to add (if absent) or update (if a column of that name exists).
            verbose_name: Display name shown in the UI.
            description: Column description.
            expression: SQL expression, for a calculated column.
            type: Column type, e.g. "STRING", "INTEGER", "TIMESTAMP".
            is_dttm: Mark as a temporal column.
            filterable: Allow filtering on the column.
            groupby: Allow grouping by the column.
            is_active: Whether the column is active.

        Returns:
            JSON with action ("added", "updated" or "unchanged"), columns_total and the update result.
            "unchanged" means the column already exists and no field was given, so nothing was written.
        """
        name = column_name.strip()
        if not name:
            return json.dumps({"error": "column_name must not be empty."}, ensure_ascii=False)

        changes = {
            key: value
            for key, value in {
                "verbose_name": verbose_name,
                "description": description,
                "expression": expression,
                "type": type,
                "is_dttm": is_dttm,
                "filterable": filterable,
                "groupby": groupby,
                "is_active": is_active,
            }.items()
            if value is not None
        }

        current = (await client.get(f"/api/v1/dataset/{dataset_id}")).get("result") or {}
        # Write every existing column back (only the fields Superset accepts, and never explicit
        # nulls, which some column fields reject), so the PUT replaces the list with itself + 1.
        columns = [
            {key: col[key] for key in _WRITABLE_COLUMN_FIELDS if col.get(key) is not None}
            for col in current.get("columns") or []
        ]

        existing = next((col for col in columns if col.get("column_name") == name), None)
        if existing is not None and not changes:
            return json.dumps(
                {"action": "unchanged", "column_name": name, "columns_total": len(columns)}, ensure_ascii=False
            )
        if existing is not None:
            existing.update(changes)
            action = "updated"
        else:
            columns.append({"column_name": name, **changes})
            action = "added"

        payload = {"columns": columns}
        if current.get("always_filter_main_dttm") is not None:
            payload["always_filter_main_dttm"] = current["always_filter_main_dttm"]
        result = await client.put(f"/api/v1/dataset/{dataset_id}", json_data=payload)
        return json.dumps(
            {"action": action, "column_name": name, "columns_total": len(columns), "result": result},
            ensure_ascii=False,
        )

    @mcp.tool
    async def superset_dataset_refresh_schema(dataset_id: int) -> str:
        """Refresh the dataset schema from the source (rescan columns and types).

        Useful after ALTER TABLE or any structural change to the underlying table.

        Args:
            dataset_id: Dataset ID.

        Returns:
            JSON string with the refresh result.
        """
        result = await client.put(f"/api/v1/dataset/{dataset_id}/refresh", json_data={})
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_dataset_delete(
        dataset_id: int,
        confirm_delete: bool = False,
    ) -> str:
        """Delete a dataset. Charts using this dataset will stop working.

        CRITICAL: deleting a dataset breaks all linked charts and dashboards.

        Args:
            dataset_id: ID of the dataset to delete.
            confirm_delete: Deletion confirmation (REQUIRED).

        Returns:
            JSON string with the deletion result.
        """
        if not confirm_delete:
            try:
                ds_info = await client.get(f"/api/v1/dataset/{dataset_id}")
                ds_name = ds_info.get("result", {}).get("table_name", "?")
                related = await client.get(f"/api/v1/dataset/{dataset_id}/related_objects/")
                charts_count = related.get("charts", {}).get("count", 0)
                dashboards_count = related.get("dashboards", {}).get("count", 0)
            except Exception:
                ds_name = f"ID={dataset_id}"
                charts_count = dashboards_count = "?"
            return json.dumps(
                {
                    "error": (
                        f"REJECTED: deleting dataset '{ds_name}' (ID={dataset_id}) "
                        f"will break {charts_count} charts and {dashboards_count} dashboards. "
                        f"Pass confirm_delete=True to confirm."
                    )
                },
                ensure_ascii=False,
            )

        result = await client.delete(f"/api/v1/dataset/{dataset_id}")
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_dataset_duplicate(
        base_model_id: int,
        table_name: str,
    ) -> str:
        """Create a copy of an existing dataset (with columns and metrics).

        Args:
            base_model_id: ID of the source dataset to copy.
                IMPORTANT: the field is called base_model_id, NOT base_id or dataset_id.
            table_name: Name for the new dataset (must be unique).

        Returns:
            JSON string with the duplicated dataset details.
        """
        result = await client.post(
            "/api/v1/dataset/duplicate",
            json_data={"base_model_id": base_model_id, "table_name": table_name},
        )
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_dataset_related_objects(dataset_id: int) -> str:
        """Get objects related to a dataset (charts and dashboards).

        Useful before deleting a dataset to understand the impact.

        Args:
            dataset_id: Dataset ID.

        Returns:
            JSON string with related charts and dashboards.
        """
        result = await client.get(f"/api/v1/dataset/{dataset_id}/related_objects/")
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_dataset_export(
        dataset_ids: str,
    ) -> str:
        """Export datasets with dependencies (databases) to a ZIP file.

        The result is a base64-encoded ZIP. Can be imported via dataset_import.

        Args:
            dataset_ids: Comma-separated dataset IDs (e.g. "1,2,3").

        Returns:
            JSON: {"format": "zip", "encoding": "base64", "data": "...", "size_bytes": N}
        """
        params = {"q": f"[{dataset_ids}]"}
        raw = await client.get_raw("/api/v1/dataset/export/", params=params)
        return json.dumps(
            {
                "format": "zip",
                "encoding": "base64",
                "data": base64.b64encode(raw).decode(),
                "size_bytes": len(raw),
            },
            ensure_ascii=False,
        )

    @mcp.tool
    async def superset_dataset_import(
        file_path: str,
        overwrite: bool = False,
    ) -> str:
        """Import datasets from a ZIP file (created via export).

        Args:
            file_path: Absolute path to the ZIP file on disk.
            overwrite: Overwrite existing objects with matching UUIDs (default False).

        Returns:
            JSON string with the import result.
        """
        with open(file_path, "rb") as f:
            files = {"formData": (file_path.split("/")[-1], f, "application/zip")}
            data = {"overwrite": "true" if overwrite else "false"}
            result = await client.post_form(
                "/api/v1/dataset/import/",
                files=files,
                data=data,
            )
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool
    async def superset_dataset_get_or_create(
        database_id: int,
        table_name: str,
        schema_name: str | None = None,
    ) -> str:
        """Get an existing dataset or create a new one for a table.

        If a dataset for the specified table already exists, returns it.
        If not, creates a new physical dataset.

        Args:
            database_id: Database connection ID (from superset_database_list).
            table_name: Table name in the database.
            schema_name: Database schema (e.g. "public", "source"). If omitted, uses the DB default schema.

        Returns:
            JSON string with the dataset details.
        """
        payload = {"database_id": database_id, "table_name": table_name}
        if schema_name is not None:
            payload["schema"] = schema_name
        result = await client.post(
            "/api/v1/dataset/get_or_create/",
            json_data=payload,
        )
        return json.dumps(result, ensure_ascii=False)

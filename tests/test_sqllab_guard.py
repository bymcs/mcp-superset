"""The DDL/DML guard in superset_sqllab_execute.

CHANGELOG 0.2.6 fixed six ways to slip a modifying statement past it (CTE, chaining, parentheses,
EXPLAIN, COPY, DO/EXECUTE blocks) and none of them had a test. The guard is a best-effort safety net,
not a parser (the authoritative protection is ``allow_dml=false`` on the database connection), so the
point of these tests is to pin what it catches today and to keep identifiers and quoted text from being
rejected by mistake.
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
from mcp_superset.tools.queries import _detect_dangerous_sql

BASE = "https://superset.example.com"

BLOCKED = [
    # (statement, keyword the rejection must name)
    ("DROP TABLE t", "DROP"),
    ("drop table t", "DROP"),
    ("DrOp TaBlE t", "DROP"),
    ("TRUNCATE TABLE t", "TRUNCATE"),
    ("DELETE FROM t", "DELETE"),
    ("UPDATE t SET a = 1", "UPDATE"),
    ("INSERT INTO t VALUES (1)", "INSERT"),
    ("ALTER TABLE t ADD c int", "ALTER"),
    ("CREATE TABLE t (a int)", "CREATE"),
    ("GRANT ALL ON t TO u", "GRANT"),
    ("REVOKE ALL ON t FROM u", "REVOKE"),
    ("MERGE INTO t USING s ON 1=1 WHEN MATCHED THEN DELETE", "MERGE"),
    ("COPY t TO '/tmp/x'", "COPY"),
    # --- the 0.2.6 bypasses
    ("WITH x AS (SELECT 1) DELETE FROM t", "DELETE"),  # CTE
    ("SELECT 1; DROP TABLE t", "DROP"),  # chaining
    ("(DELETE FROM t)", "DELETE"),  # parentheses
    ("EXPLAIN ANALYZE DELETE FROM t", "DELETE"),  # EXPLAIN prefix
    # DDL hidden in a string inside a PL/pgSQL block. The $$ body stays visible to the scan, so the
    # rejection names the DROP inside it; a block with nothing recognisable is caught by DO itself.
    ("DO $$ BEGIN EXECUTE 'DROP TABLE t'; END $$", "DROP"),
    ("DO $$ BEGIN PERFORM 1; END $$", "DO"),
    # --- comments and whitespace must not hide the keyword
    ("/* harmless */ DROP TABLE t", "DROP"),
    ("-- harmless\nDROP TABLE t", "DROP"),
    ("SELECT 1\n;\nDROP\nTABLE t", "DROP"),
    ("SELECT 1;\tDROP\tTABLE t", "DROP"),
    ("SELECT 1 /* x */ ; /* y */ DELETE FROM t", "DELETE"),
    # --- a comment marker inside a quoted region must not swallow what follows it (the D9 bypass:
    # comments used to be stripped before literals, so the guard saw only "SELECT '")
    ("SELECT '--'; DROP TABLE t", "DROP"),
    ("SELECT '/*'; DROP TABLE t; SELECT '*/'", "DROP"),
    ("SELECT 'it''s --'; DROP TABLE t", "DROP"),
    ("SELECT '-- '; DROP TABLE t", "DROP"),  # '--' followed by a space is a real comment marker
    ('SELECT "-- "; DROP TABLE t', "DROP"),  # double-quoted identifier / MySQL string
    ('SELECT "/*"; DROP TABLE t; SELECT "*/"', "DROP"),
    ("SELECT `-- `; DROP TABLE t", "DROP"),  # MySQL identifier
    ("SELECT `/*`; DROP TABLE t; SELECT `*/`", "DROP"),
    ("SELECT [-- ]; DROP TABLE t", "DROP"),  # SQL Server identifier
    ("SELECT [/*]; DROP TABLE t; SELECT [*/]", "DROP"),
    ("SELECT $$-- $$; DROP TABLE t", "DROP"),  # PostgreSQL dollar quoting
    ("SELECT $tag$/*$tag$; DROP TABLE t; SELECT '*/'", "DROP"),
    # --- engine differences in what counts as a comment must never hide a statement
    ("SELECT 5--1; DROP TABLE t", "DROP"),  # MySQL: '--' is a comment only before whitespace
    ("SELECT 1 /*!50000 DROP TABLE t */", "DROP"),  # MySQL executes /*! ... */ conditional comments
    ("SELECT 1 /*!-- x */ UPDATE t SET a = 1", "UPDATE"),  # a '-- ' inside one must not eat the closing */
    ("SELECT 1 /*!*/ ; /*!-- */ DELETE FROM t", "DELETE"),
    # --- backslash is NOT an escape: PostgreSQL/SQL Server read this as a closed literal followed by a
    # DROP; MySQL would read one long literal. Flagging is the safe side of that disagreement.
    ("SELECT '\\'; DROP TABLE t; -- '", "DROP"),
    # --- anything unterminated is scanned as code rather than assumed to be quoted text
    ("SELECT 'x; DROP TABLE t", "DROP"),
    ("SELECT 1 /* ; DROP TABLE t", "DROP"),
    # --- a quote inside a comment must not open a string that hides the next statement
    ("SELECT 1 -- it's here\n; DROP TABLE t", "DROP"),
    ("SELECT 1 /* it's here */; DROP TABLE t", "DROP"),
]

ALLOWED = [
    "SELECT * FROM t",
    "SELECT count(*) FROM public.my_table LIMIT 10",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    # Word boundaries: identifiers that merely contain a dangerous word.
    "SELECT update_date, deleted_at, call_datetime, dropdown, doing, created_by FROM t",
    # Keywords inside string literals or comments are data, not statements.
    "SELECT 'DROP TABLE t' AS s",
    "SELECT 'it''s a DELETE' AS s",  # SQL-standard escaped quote
    "SELECT 1 -- drop table t",
    "SELECT /* delete */ 1",
    "SELECT 1 /* multi\nline DROP\n*/",
    # Comment markers that live inside quoted text are data.
    "SELECT 'a--b' AS s, 1 -- real comment with DROP",
    "SELECT '/*' AS a, '*/' AS b",
    'SELECT "col-- x" FROM t',
    "SELECT [a-- b] FROM t",
    'SELECT "it\'s" FROM t',  # an apostrophe in a quoted identifier is not a string opener
    "SELECT 1 -- it's a comment",
    "SELECT 1 /* it's */ + 2",
    "SELECT 1 --\n, 2",  # '--' directly before a line break is still a comment
]


@pytest.mark.parametrize(("sql", "keyword"), BLOCKED)
def test_dangerous_statements_are_detected(sql, keyword):
    assert _detect_dangerous_sql(sql) == keyword


@pytest.mark.parametrize("sql", ALLOWED)
def test_harmless_statements_are_not_flagged(sql):
    assert _detect_dangerous_sql(sql) is None


# ------------------------------------------------------------ through the tool


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


def _mock_execute():
    respx.get(f"{BASE}/api/v1/security/csrf_token/").mock(return_value=httpx.Response(200, json={"result": "csrf"}))
    return respx.post(f"{BASE}/api/v1/sqllab/execute/").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": [{"a": 1}]})
    )


async def _execute(mcp_server, args):
    async with Client(mcp_server) as c:
        return json.loads(_text(await c.call_tool("superset_sqllab_execute", {"database_id": 3, **args})))


@respx.mock
@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE t",
        "WITH x AS (SELECT 1) DELETE FROM t",
        "SELECT 1; TRUNCATE t",
        "SELECT '--'; DROP TABLE t",  # D9: reaches the tool, not just the helper
    ],
)
async def test_rejected_sql_never_reaches_superset(mcp_server, sql):
    execute = _mock_execute()

    out = await _execute(mcp_server, {"sql": sql})

    assert out["error"].startswith("REJECTED: SQL query contains")
    assert "SQL Lab in the Superset UI" in out["error"]  # tells the user how to proceed legitimately
    assert not execute.called


@respx.mock
async def test_allowed_sql_is_sent_unchanged_with_a_row_limit_and_no_async(mcp_server):
    execute = _mock_execute()
    sql = "SELECT update_date FROM t -- trailing comment kept as typed"

    out = await _execute(mcp_server, {"sql": sql})

    assert out["status"] == "success"
    body = json.loads(execute.calls.last.request.content)
    assert body == {"database_id": 3, "sql": sql, "runAsync": False, "queryLimit": 1000}


@respx.mock
async def test_optional_arguments_are_mapped_to_the_sqllab_payload(mcp_server):
    execute = _mock_execute()

    await _execute(
        mcp_server,
        {
            "sql": "SELECT 1",
            "schema": "public",
            "catalog": "main",
            "tab_name": "my tab",
            "template_params": '{"d": "2024-01-01"}',
        },
    )

    body = json.loads(execute.calls.last.request.content)
    assert (body["schema"], body["catalog"], body["tab"]) == ("public", "main", "my tab")
    assert body["templateParams"] == '{"d": "2024-01-01"}'

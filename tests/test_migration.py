"""Verifies the Alembic migration actually applies cleanly against Postgres.

Runs against its own scratch database so it doesn't interfere with the
metadata-based schema the other tests use.
"""

import os
import subprocess
import sys

import psycopg2
import pytest

_MIGRATION_TEST_DB = "sense_tool_migration_test"
_ADMIN_DSN = dict(host="localhost", port=5432, user="sense_tool", password="sense_tool", dbname="postgres")


def _run_alembic(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["ALEMBIC_DATABASE_URL"] = (
        f"postgresql+psycopg2://sense_tool:sense_tool@localhost:5432/{_MIGRATION_TEST_DB}"
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module", autouse=True)
def _scratch_database():
    conn = psycopg2.connect(**_ADMIN_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS " + _MIGRATION_TEST_DB)
        cur.execute("CREATE DATABASE " + _MIGRATION_TEST_DB)
    conn.close()
    yield
    conn = psycopg2.connect(**_ADMIN_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS " + _MIGRATION_TEST_DB)
    conn.close()


def test_alembic_upgrade_downgrade_upgrade_round_trip():
    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr

    conn = psycopg2.connect(
        host="localhost", port=5432, user="sense_tool", password="sense_tool", dbname=_MIGRATION_TEST_DB
    )
    with conn.cursor() as cur:
        cur.execute(
            "select column_name from information_schema.columns where table_name='documents'"
        )
        columns = {row[0] for row in cur.fetchall()}
    conn.close()
    assert {
        "id",
        "source",
        "status",
        "raw_file_path",
        "extracted_text",
        "structured_data",
        "image_regions",
        "text_source",
        "ocr_lang",
        "searchable_pdf_key",
        "error_message",
        "search_vector",
        "created_at",
        "updated_at",
    }.issubset(columns)

    result = _run_alembic("downgrade", "base")
    assert result.returncode == 0, result.stderr

    result = _run_alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr

    # 0002 down-revisions to 0001, not base - check the column is
    # add/drop-reversible on its own too.
    assert _run_alembic("downgrade", "0001").returncode == 0
    conn = psycopg2.connect(
        host="localhost", port=5432, user="sense_tool", password="sense_tool", dbname=_MIGRATION_TEST_DB
    )
    with conn.cursor() as cur:
        cur.execute(
            "select column_name from information_schema.columns where table_name='documents'"
        )
        cols_at_0001 = {row[0] for row in cur.fetchall()}
    conn.close()
    assert "image_regions" not in cols_at_0001
    assert _run_alembic("upgrade", "head").returncode == 0

    # 0003 down-revisions to 0002 - its three WP-G columns must be
    # add/drop-reversible on their own too.
    assert _run_alembic("downgrade", "0002").returncode == 0
    conn = psycopg2.connect(
        host="localhost", port=5432, user="sense_tool", password="sense_tool", dbname=_MIGRATION_TEST_DB
    )
    with conn.cursor() as cur:
        cur.execute(
            "select column_name from information_schema.columns where table_name='documents'"
        )
        cols_at_0002 = {row[0] for row in cur.fetchall()}
    conn.close()
    assert {"text_source", "ocr_lang", "searchable_pdf_key"}.isdisjoint(cols_at_0002)
    assert "image_regions" in cols_at_0002
    assert _run_alembic("upgrade", "head").returncode == 0

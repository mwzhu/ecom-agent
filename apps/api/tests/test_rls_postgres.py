from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from api.config import get_settings

RLS_TEST_ROLE = "ecom_agent_rls_tester"


def _postgres_container_url() -> Iterator[str]:
    try:
        import docker
        from docker.errors import DockerException
        from testcontainers.postgres import PostgresContainer
    except ImportError as exc:
        pytest.skip(f"Postgres RLS test dependencies are unavailable: {exc}")

    try:
        docker.from_env().ping()
    except DockerException as exc:
        pytest.skip(f"Docker is not available for Postgres RLS test: {exc}")

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as postgres:
        yield postgres.get_connection_url()


def test_postgres_rls_blocks_bare_cross_tenant_case_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    database_urls = _postgres_container_url()
    if not hasattr(database_urls, "__next__"):
        pytest.skip("Postgres container did not initialize.")

    database_url = next(database_urls)
    try:
        monkeypatch.setenv("DATABASE_URL", database_url)
        get_settings.cache_clear()

        alembic_config = Config(str(Path("infra/alembic.ini")))
        command.upgrade(alembic_config, "head")

        psycopg_url = _psycopg_url(database_url)
        _bootstrap_rls_test_role(psycopg_url)
        merchant_a = _insert_merchant(psycopg_url, "org_a", "Merchant A")
        merchant_b = _insert_merchant(psycopg_url, "org_b", "Merchant B")
        _insert_case(psycopg_url, merchant_a, "A-1001")
        _insert_case(psycopg_url, merchant_b, "B-2001")

        with psycopg.connect(psycopg_url) as connection:
            with connection.cursor() as cursor:
                _assume_rls_test_role(cursor)
                cursor.execute(
                    "select set_config('app.merchant_id', %s, false)",
                    (str(merchant_a),),
                )
                cursor.execute("select subject_ref->>'order_id' from cases order by created_at")
                rows = cursor.fetchall()

        assert rows == [("A-1001",)]
    finally:
        get_settings.cache_clear()
        try:
            next(database_urls)
        except StopIteration:
            pass


def _psycopg_url(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _insert_merchant(database_url: str, clerk_org_id: str, name: str) -> UUID:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            _assume_rls_test_role(cursor)
            cursor.execute(
                "insert into merchants (clerk_org_id, name) values (%s, %s) returning id",
                (clerk_org_id, name),
            )
            row = cursor.fetchone()
    if row is None:
        raise AssertionError("Merchant insert did not return an id.")
    return UUID(str(row[0]))


def _insert_case(database_url: str, merchant_id: UUID, order_id: str) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            _assume_rls_test_role(cursor)
            cursor.execute("select set_config('app.merchant_id', %s, false)", (str(merchant_id),))
            cursor.execute(
                """
                insert into cases (merchant_id, type, subject_ref)
                values (%s, 'fraud_triage', jsonb_build_object('order_id', %s::text))
                """,
                (merchant_id, order_id),
            )


def _bootstrap_rls_test_role(database_url: str) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                do $$
                begin
                  if not exists (select 1 from pg_roles where rolname = '{RLS_TEST_ROLE}') then
                    create role {RLS_TEST_ROLE} nosuperuser login password 'test-password';
                  end if;
                end
                $$;
                """
            )
            cursor.execute(f"grant usage on schema public to {RLS_TEST_ROLE}")
            cursor.execute(
                f"grant select, insert, update, delete on merchants, cases to {RLS_TEST_ROLE}"
            )


def _assume_rls_test_role(cursor: psycopg.Cursor[object]) -> None:
    cursor.execute(f"set session authorization {RLS_TEST_ROLE}")

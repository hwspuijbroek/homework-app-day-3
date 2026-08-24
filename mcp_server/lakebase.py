"""
Lakebase (Databricks-managed Postgres) connection helper.

Connects using a single LAKEBASE_URL (a standard Postgres connection URL,
e.g. postgresql://role:password@host:5432/databricks_postgres?sslmode=require)
pointing at a native Postgres role with a static, non-expiring password.
This keeps setup to a single secret instead of five separate env vars.
"""

import base64
import os
from contextlib import contextmanager
from functools import lru_cache

import psycopg2
from psycopg2.extras import RealDictCursor

_SCOPE = os.environ.get("LAKEBASE_SECRET_SCOPE", "database")
_KEY = os.environ.get("LAKEBASE_SECRET_KEY", "lakebase-url")


@lru_cache(maxsize=1)
def _workspace_client():
    """
    The Databricks client, created on first use.

    Building it at import time meant importing this module — which every test does,
    directly or not — required working workspace credentials. `pytest` then died
    during collection on any machine without them, while the README promised the
    suite needs no live connection.
    """
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


@lru_cache(maxsize=1)
def _lakebase_url() -> str:
    """
    Fetch and decode the Lakebase connection URL from the Databricks secret scope.

    Cached: this used to run per connection, and re-embedding after a sync opens
    one connection per document — roughly fifty Secrets API round-trips for a
    single /weather/sync.
    """
    secret = _workspace_client().secrets.get_secret(scope=_SCOPE, key=_KEY)
    return base64.b64decode(secret.value).decode("utf-8")


def _connect(max_retries: int = 3, base_delay: int = 5):
    """
    Open a connection, retrying while Lakebase wakes from auto-suspend.

    Only the connect is retried. Retrying *around* the caller's work would resume
    the generator after it had already yielded, which raises
    "generator didn't stop after throw()" and masks the real error.
    """
    import logging
    import time

    logger = logging.getLogger(__name__)

    for attempt in range(max_retries):
        try:
            return psycopg2.connect(
                _lakebase_url(), cursor_factory=RealDictCursor,
                # Without these, an idle connection is silently dropped by the
                # network long before Postgres notices, and the next query dies
                # with "SSL SYSCALL error: EOF detected" — observed here, and
                # far more likely against a Lakebase that auto-suspends.
                keepalives=1, keepalives_idle=30,
                keepalives_interval=10, keepalives_count=5,
                connect_timeout=15)
        except psycopg2.OperationalError as e:
            error_msg = str(e)
            lowered = error_msg.lower()
            is_asleep = ("endpoint has been disabled" in lowered
                         or "enable it using the api" in lowered)
            # A connection that breaks *while being made* was not retried at
            # all, only a suspended endpoint was. So "SSL SYSCALL error: EOF
            # detected" during connect went straight to the caller as a 500, and
            # it blocked a deploy: seven questions failed on one dropped TLS
            # handshake against a Lakebase that auto-suspends and resumes.
            #
            # Bad credentials or a missing database are not retried — those do
            # not get better by waiting, and retrying them only turns a clear
            # error into a slow one.
            is_transient = any(sign in lowered for sign in (
                "ssl syscall error", "eof detected", "connection reset",
                "connection refused", "timeout expired", "could not connect",
                "server closed the connection", "temporary failure in name resolution",
            ))

            if (is_asleep or is_transient) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                reden = "endpoint is disabled/starting" if is_asleep else "connection dropped"
                logger.warning(
                    f"Lakebase {reden}. Retrying in {delay}s… "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(delay)
                continue

            logger.error(f"Database connection error: {error_msg}")
            raise


@contextmanager
def get_connection():
    """
    Yield a raw psycopg2 connection with a RealDictCursor factory.

    The connection is checked before it is handed over. `_connect` retries a
    *refused* connection, but a connection that Lakebase drops while idle fails
    later, inside the caller's query, with "SSL SYSCALL error: EOF detected" —
    and that is not something the caller can sensibly handle. One cheap
    round-trip here turns a class of intermittent 500s into nothing at all.

    Retrying around the `yield` is deliberately not done: resuming a generator
    after it has yielded raises "generator didn't stop after throw()" and buries
    the real error.
    """
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    except psycopg2.Error:
        conn.close()
        conn = _connect()

    try:
        yield conn
    finally:
        conn.close()


def ensure_schema() -> None:
    """Apply migrations/*.sql against Lakebase. Idempotent (CREATE ... IF NOT EXISTS)."""
    migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
    if not os.path.isdir(migrations_dir):
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            for filename in sorted(os.listdir(migrations_dir)):
                if not filename.endswith(".sql"):
                    continue
                with open(os.path.join(migrations_dir, filename)) as f:
                    cur.execute(f.read())
            conn.commit()

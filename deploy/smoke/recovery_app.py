"""Synthetic process for recovery rehearsal. Never imports the real bootstrap."""

from __future__ import annotations

import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import psycopg

DSN = "postgresql://rehearsal:placeholder@db/rehearsal"


def main() -> int:
    mode = sys.argv[1]
    if mode == "healthcheck":
        if Path("/app/fail-health").exists():
            return 1
        with psycopg.connect(DSN) as connection:
            connection.execute("SELECT 1")
        return 0
    if mode == "migrate":
        with psycopg.connect(DSN) as connection:
            connection.execute("CREATE SCHEMA IF NOT EXISTS app")
            connection.execute("CREATE TABLE IF NOT EXISTS app.alembic_version (version_num text)")
            connection.execute("DELETE FROM app.alembic_version")
            connection.execute(
                "INSERT INTO app.alembic_version VALUES (%s)",
                (Path("/app/heads").read_text().strip(),),
            )
            connection.execute("CREATE SCHEMA IF NOT EXISTS procrastinate")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS procrastinate.procrastinate_workers "
                "(last_heartbeat timestamptz NOT NULL)"
            )
        return 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"synthetic previous image serving")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    stopping = threading.Event()
    server = HTTPServer(("127.0.0.1", 8080), Handler)
    server.timeout = 0.2
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    with psycopg.connect(DSN) as connection:
        connection.execute("DELETE FROM procrastinate.procrastinate_workers")
        connection.execute(
            "INSERT INTO procrastinate.procrastinate_workers SELECT now() FROM generate_series(1,3)"
        )
    while not stopping.is_set():
        with psycopg.connect(DSN) as connection:
            connection.execute(
                "UPDATE procrastinate.procrastinate_workers SET last_heartbeat=now()"
            )
        server.handle_request()
    server.server_close()
    with psycopg.connect(DSN) as connection:
        connection.execute("DELETE FROM procrastinate.procrastinate_workers")
    return 0


if __name__ == "__main__":
    # Ignore real developer settings even if the caller has them exported.
    os.environ.pop("BOT_TOKEN", None)
    os.environ.pop("OPENAI_API_KEY", None)
    raise SystemExit(main())

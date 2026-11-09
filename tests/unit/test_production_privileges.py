"""Production credentials must follow service boundaries."""

import re
from pathlib import Path

import psycopg
import pytest

from assistant.db import production_roles

ROOT = Path(__file__).resolve().parents[2]


def test_compose_uses_separate_environment_files_without_credential_overrides() -> None:
    compose = (ROOT / "deploy/compose.prod.yml").read_text("utf-8")
    for service, filename in {
        "app": ".env.app",
        "migrate": ".env.migrate",
        "backup": ".env.backup",
        "db-roles": ".env.roles",
        "restore": ".env.restore",
    }.items():
        match = re.search(rf"^  {service}:\n(.*?)(?=^  \S|^volumes:)", compose, re.M | re.S)
        assert match is not None
        block = match.group(1)
        assert f"env_file: ../{filename}\n" in block
        assert "${POSTGRES_PASSWORD" not in block
        assert "DATABASE_URL:" not in block
        assert "PGPASSWORD:" not in block


def test_runtime_does_not_install_the_procrastinate_schema() -> None:
    jobs = (ROOT / "src/assistant/jobs/app.py").read_text("utf-8")
    bootstrap = (ROOT / "src/assistant/bootstrap.py").read_text("utf-8")
    for source in (jobs, bootstrap):
        assert "apply_schema" not in source
        assert "create_all" not in source


def test_role_preflight_reports_only_error_type_and_runbook(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "SYNTHETIC-PRIVATE-DB-DIAGNOSTIC"

    def reject(url: str, passwords: production_roles.RolePasswords) -> None:
        raise psycopg.OperationalError(secret)

    monkeypatch.setattr(production_roles, "verify_roles", reject)
    monkeypatch.setattr("sys.argv", ["production_roles", "check"])
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://synthetic@db/assistant")
    for key in ("MIGRATION_DB_PASSWORD", "APP_DB_PASSWORD", "BACKUP_DB_PASSWORD"):
        monkeypatch.setenv(key, "a" * 64)
    assert production_roles.main() == 1
    output = capsys.readouterr()
    assert "OperationalError" in output.err
    assert "I24 role upgrade" in output.err
    assert secret not in output.err
    assert "postgresql" not in output.err
    assert output.out == ""

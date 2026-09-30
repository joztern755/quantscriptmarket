#!/usr/bin/env python3
"""Apply backend/migrations/NNNN_name.sql in order (SPEC §3).

* Tracks applied files in ``schema_migrations(version, sha256, applied_at, applied_by)``.
* REFUSES to run if an already-applied file's sha256 changed, or an applied file disappeared
  (never edit an applied migration — add a new one).
* Each migration runs in ONE transaction together with its ``schema_migrations`` row, under a
  transaction-scoped advisory lock (two concurrent deploys cannot both apply the same file).
* Migrations after the bootstrap run as ``SET LOCAL ROLE app_migrator`` when that role exists and the
  connecting user is a member (so app_migrator owns every object). A file whose header contains the line
  ``-- migrate:session-user`` runs as the connecting user instead (0001/0002: they create the roles and
  move ownership, and need CREATEROLE).
* Driver: psycopg (v3) when importable, otherwise the ``psql`` CLI (``--driver`` to force one).

Usage:
    python backend/scripts/migrate.py [--database-url URL] [--status] [--dry-run]
                                      [--dir DIR] [--role app_migrator|''] [--driver auto|psycopg|psql]
The URL defaults to app.config.get_settings().database_url (env DATABASE_URL).
Exit codes: 0 ok, 1 migration/SQL error, 2 checksum/consistency refusal.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DIR = BACKEND_DIR / "migrations"
LOCK_NAME = "aijalon.migrate"
NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
SESSION_USER_DIRECTIVE = re.compile(r"^--\s*migrate:session-user\s*$", re.MULTILINE)

CREATE_TRACKING_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    sha256      text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at  timestamptz NOT NULL DEFAULT now(),
    applied_by  text NOT NULL DEFAULT session_user
)
"""


class MigrationError(Exception):
    """SQL failure while applying a migration (exit 1)."""


class ChecksumRefusal(Exception):
    """Applied history does not match the files on disk (exit 2)."""


@dataclass(frozen=True)
class Migration:
    version: str          # file stem, e.g. "0001_init"
    path: Path
    sha256: str
    sql: str

    @property
    def session_user_only(self) -> bool:
        head = "\n".join(self.sql.splitlines()[:40])
        return bool(SESSION_USER_DIRECTIVE.search(head))


def discover(directory: Path) -> list[Migration]:
    found: list[Migration] = []
    numbers: dict[str, str] = {}
    for p in sorted(directory.glob("*.sql")):
        m = NAME_RE.match(p.name)
        if not m:
            raise ChecksumRefusal(f"bad migration file name {p.name!r} (expected NNNN_lower_snake.sql)")
        if m.group(1) in numbers:
            raise ChecksumRefusal(f"duplicate migration number {m.group(1)}: {numbers[m.group(1)]} and {p.name}")
        numbers[m.group(1)] = p.name
        raw = p.read_bytes()
        text = raw.decode("utf-8")
        if re.search(r"^\s*(BEGIN|COMMIT|ROLLBACK)\s*;", text, re.IGNORECASE | re.MULTILINE):
            raise ChecksumRefusal(f"{p.name}: must not contain BEGIN/COMMIT/ROLLBACK (runner wraps each file)")
        found.append(Migration(p.stem, p, hashlib.sha256(raw).hexdigest(), text))
    return found


def plan(migrations: list[Migration], applied: dict[str, str]) -> list[Migration]:
    """Return pending migrations; raise ChecksumRefusal if history and files disagree."""
    by_version = {m.version: m for m in migrations}
    problems = []
    for version, sha in sorted(applied.items()):
        m = by_version.get(version)
        if m is None:
            problems.append(f"{version}: applied in the database but missing on disk")
        elif m.sha256 != sha:
            problems.append(f"{version}: checksum changed since it was applied (db {sha[:12]}…, file {m.sha256[:12]}…)")
    if problems:
        raise ChecksumRefusal("refusing to migrate:\n  " + "\n  ".join(problems))
    pending = [m for m in migrations if m.version not in applied]
    if pending and applied:
        last_applied = max(applied)
        early = [m.version for m in pending if m.version < last_applied]
        if early:
            raise ChecksumRefusal(f"refusing to migrate: {early} sort before already-applied {last_applied}")
    return pending


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _libpq_url(url: str) -> str:
    """SQLAlchemy URLs (postgresql+psycopg://) -> libpq URL."""
    return re.sub(r"^postgres(ql)?\+[a-z0-9_]+://", "postgresql://", url)


class Driver(Protocol):
    def ensure_tracking_table(self) -> None: ...
    def applied(self) -> dict[str, str]: ...
    def can_set_role(self, role: str) -> bool: ...
    def apply(self, m: Migration, role: str | None) -> None: ...


def _preamble(m: Migration, role: str | None) -> str:
    lines = [
        f"SELECT pg_advisory_xact_lock(hashtext({_sql_literal(LOCK_NAME)}));",
        "DO $migrate$ BEGIN IF EXISTS (SELECT 1 FROM schema_migrations WHERE version = "
        f"{_sql_literal(m.version)}) THEN RAISE EXCEPTION 'migration % already applied (concurrent run?)', "
        f"{_sql_literal(m.version)}; END IF; END $migrate$;",
    ]
    if role:
        lines.append(f'SET LOCAL ROLE "{role}";')
    return "\n".join(lines) + "\n"


def _postamble(m: Migration) -> str:
    return (f"\n;\nINSERT INTO schema_migrations (version, sha256) VALUES "
            f"({_sql_literal(m.version)}, {_sql_literal(m.sha256)});\n")


_CAN_SET_ROLE_SQL = ("SELECT (EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {r}) "
                     "AND pg_has_role(current_user, {r}, 'MEMBER'))::text")


class PsqlDriver:
    def __init__(self, url: str, psql: str = "psql") -> None:
        exe = shutil.which(psql)
        if not exe:
            raise MigrationError("psql not found on PATH (install postgresql-client or psycopg)")
        self.url, self.exe = url, exe

    def _run(self, script: str, *, tuples: bool = False) -> str:
        args = [self.exe, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", self.url, "-f", "-"]
        if tuples:
            args[1:1] = ["-A", "-t", "-F", "\t"]
        r = subprocess.run(args, input=script, capture_output=True, text=True)
        if r.returncode != 0:
            raise MigrationError(r.stderr.strip() or f"psql exited {r.returncode}")
        return r.stdout

    def ensure_tracking_table(self) -> None:
        self._run("SET client_min_messages = warning;\n" + CREATE_TRACKING_SQL + ";\n")

    def applied(self) -> dict[str, str]:
        out = self._run("SELECT version, sha256 FROM schema_migrations ORDER BY version;\n", tuples=True)
        rows = [line.split("\t") for line in out.splitlines() if line.strip()]
        return {v: s for v, s in rows}

    def can_set_role(self, role: str) -> bool:
        out = self._run(_CAN_SET_ROLE_SQL.format(r=_sql_literal(role)) + ";\n", tuples=True)
        return out.strip() == "true"

    def apply(self, m: Migration, role: str | None) -> None:
        script = "BEGIN;\n" + _preamble(m, role) + m.sql + _postamble(m) + "COMMIT;\n"
        self._run(script)


class PsycopgDriver:
    def __init__(self, url: str) -> None:
        import psycopg  # type: ignore[import-not-found]

        self.conn = psycopg.connect(url, autocommit=True)

    def ensure_tracking_table(self) -> None:
        self.conn.execute(CREATE_TRACKING_SQL)

    def applied(self) -> dict[str, str]:
        return {v: s for v, s in self.conn.execute("SELECT version, sha256 FROM schema_migrations ORDER BY version")}

    def can_set_role(self, role: str) -> bool:
        row = self.conn.execute(_CAN_SET_ROLE_SQL.format(r=_sql_literal(role))).fetchone()
        return bool(row) and row[0] == "true"

    def apply(self, m: Migration, role: str | None) -> None:
        try:
            with self.conn.transaction():
                # no parameters -> simple query protocol -> multi-statement files are fine
                self.conn.execute(_preamble(m, role) + m.sql + _postamble(m))
        except Exception as e:  # psycopg.Error
            raise MigrationError(f"{m.path.name}: {e}") from e


def make_driver(url: str, which: str) -> Driver:
    url = _libpq_url(url)
    if which in ("auto", "psycopg"):
        try:
            return PsycopgDriver(url)
        except ImportError:
            if which == "psycopg":
                raise MigrationError("psycopg is not installed") from None
    return PsqlDriver(url)


def default_database_url() -> str:
    sys.path.insert(0, str(BACKEND_DIR))
    from app.config import get_settings  # env is read only in app.config (SPEC §11)

    return get_settings().database_url


def run(url: str, directory: Path, *, role: str | None, driver: str = "auto", dry_run: bool = False,
        status: bool = False, out=sys.stdout) -> list[str]:
    migrations = discover(directory)
    d = make_driver(url, driver)
    d.ensure_tracking_table()
    applied = d.applied()
    pending = plan(migrations, applied)
    if status:
        for m in migrations:
            print(f"{'applied ' if m.version in applied else 'PENDING '} {m.version}  {m.sha256[:12]}", file=out)
        return [m.version for m in pending]
    done: list[str] = []
    for m in pending:
        use_role = None
        if role and not m.session_user_only and d.can_set_role(role):
            use_role = role
        if dry_run:
            print(f"would apply {m.version} as {use_role or 'session user'}", file=out)
            continue
        try:
            d.apply(m, use_role)
        except MigrationError as e:
            raise MigrationError(f"{m.path.name} failed; nothing from it was applied:\n{e}") from e
        print(f"applied {m.version} ({m.sha256[:12]}) as {use_role or 'session user'}", file=out)
        done.append(m.version)
    if not pending:
        print("up to date", file=out)
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--database-url", default=None, help="libpq or SQLAlchemy URL; default from app.config")
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--role", default="app_migrator", help="role to SET for non-bootstrap migrations ('' = none)")
    ap.add_argument("--driver", choices=("auto", "psycopg", "psql"), default="auto")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    url = a.database_url or default_database_url()
    try:
        run(url, a.dir, role=a.role or None, driver=a.driver, dry_run=a.dry_run, status=a.status)
    except ChecksumRefusal as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except MigrationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

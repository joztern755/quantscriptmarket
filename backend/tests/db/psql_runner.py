"""Test-only ``SqlRunner`` that shells out to ``psql`` (psycopg is not installable in every dev/CI box).

Each call is its own session and autocommits, so COMMIT-time (deferred) checks surface on the call itself.
Bind parameters (``:name``) are rendered as SQL literals — acceptable for tests, never for production.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, Mapping

from app.db.engine import DbError

_BIND = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)(?!:)")
_ERR = re.compile(r"ERROR:\s+([0-9A-Z]{5}):\s*(.*)")


def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (dict, list)):
        v = json.dumps(v)
    return "'" + str(v).replace("'", "''") + "'"


def psql_available() -> bool:
    return shutil.which("psql") is not None


class PsqlRunner:
    def __init__(self, url: str) -> None:
        self.url = url

    def render(self, sql: str, params: Mapping[str, Any] | None) -> str:
        params = dict(params or {})

        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in params:
                raise KeyError(f"missing bind parameter {name!r}")
            return _lit(params[name])

        return _BIND.sub(sub, sql)

    def fetchall(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        body = self.render(sql, params).strip().rstrip(";")
        returns_rows = re.match(r"^\s*(SELECT|WITH)\b", body, re.IGNORECASE) is not None
        if returns_rows:
            body = f"SELECT coalesce(json_agg(q), '[]') FROM ({body}) q"
        script = "\\set VERBOSITY verbose\n" + body + ";\n"
        r = subprocess.run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-d", self.url, "-f", "-"],
                           input=script, capture_output=True, text=True)
        if r.returncode != 0:
            m = _ERR.search(r.stderr)
            raise DbError(m.group(1) if m else None, (m.group(2) if m else r.stderr).strip())
        if not returns_rows:
            return []
        out = r.stdout.strip()
        return json.loads(out) if out else []

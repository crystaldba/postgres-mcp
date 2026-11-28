"""Tests that docker-entrypoint.sh never prints database passwords.

The script describes connection strings using libpq's own parser (via
psycopg) and prints only non-secret fields.

The script is run directly with bash and a stub command in place of
postgres-mcp, so no Docker or network access is needed. A fake ``ping`` is put
on PATH to choose between the "Docker host found" and "cannot determine
Docker host" paths deterministically.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker-entrypoint.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required to run docker-entrypoint.sh")

# A stub program that records what it received, so the test can check that the
# real (unmasked, remapped) values still reach the program.
RECORDER = """
import json, os, sys
with open(os.environ["RECORD_FILE"], "w") as f:
    json.dump({"argv": sys.argv[1:], "database_uri": os.environ.get("DATABASE_URI")}, f)
"""

PASSWORDS = {
    "env": "EnvPw-s3cr3t:#?$!%25*",
    "arg": "ArgPw-s3cr3t",
    "kv": "KvPw-s3cr3t",
    "query": "QueryPw-s3cr3t",
}


def _make_ping(bin_dir: Path, succeeds: bool) -> None:
    ping = bin_dir / "ping"
    ping.write_text(f"#!/bin/sh\nexit {0 if succeeds else 1}\n")
    ping.chmod(ping.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run(
    tmp_path: Path, ping_succeeds: bool, args: list[str], database_uri: str, no_python: bool = False
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_ping(bin_dir, ping_succeeds)
    recorder = tmp_path / "recorder.py"
    recorder.write_text(RECORDER)
    record_file = tmp_path / "record.json"
    assert BASH is not None

    # ``python`` must resolve to an interpreter that has psycopg, as it does in the image
    python_dir = "" if no_python else f"{Path(sys.executable).parent}{os.pathsep}"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{python_dir}{'' if no_python else os.environ.get('PATH', '')}",
        "DATABASE_URI": database_uri,
        "RECORD_FILE": str(record_file),
    }
    proc = subprocess.run(
        [BASH, str(ENTRYPOINT), sys.executable, str(recorder), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc, json.loads(record_file.read_text())


def _assert_no_passwords(proc: subprocess.CompletedProcess[str], *extra: str) -> None:
    output = proc.stdout + proc.stderr
    for secret in (*PASSWORDS.values(), *extra):
        assert secret not in output


@pytest.mark.parametrize("ping_succeeds", [True, False], ids=["docker-host-found", "docker-host-unknown"])
def test_entrypoint_hides_passwords_but_passes_real_values(tmp_path: Path, ping_succeeds: bool) -> None:
    env_uri = f"postgresql://envuser:{PASSWORDS['env']}@localhost:5432/envdb"
    arg_uri = f"postgresql://arguser:{PASSWORDS['arg']}@localhost/argdb"
    kv_arg = f"host=localhost password={PASSWORDS['kv']} dbname=kvdb"
    query_uri = f"postgresql://localhost/qdb?user=q&password={PASSWORDS['query']}&sslmode=disable"
    args = [arg_uri, kv_arg, query_uri, "--access-mode=restricted"]

    proc, record = _run(tmp_path, ping_succeeds, args, env_uri)

    _assert_no_passwords(proc)

    # Diagnostics are kept, as a summary of non-secret fields
    assert "Found localhost in DATABASE_URI: <connection: host=localhost port=5432 dbname=envdb user=envuser password=****>" in proc.stderr
    assert "Found localhost in database connection: <connection: host=localhost dbname=argdb user=arguser password=****>" in proc.stderr
    assert "<connection: host=localhost dbname=kvdb password=****>" in proc.stderr
    assert "--access-mode=restricted" in proc.stderr

    if ping_succeeds:
        host = "host.docker.internal"
        assert f"Remapping localhost --> {host}" in proc.stderr
        assert f"<connection: host={host} dbname=argdb user=arguser password=****>" in proc.stderr
    else:
        host = "localhost"
        assert "Cannot determine Docker host" in proc.stderr

    # The program itself still receives the real, unmasked values
    assert record["database_uri"] == env_uri.replace("localhost", host, 1)
    # (key=value strings are not rewritten by the script, only postgres:// URIs)
    assert record["argv"] == [
        arg_uri.replace("localhost", host, 1),
        kv_arg,
        query_uri.replace("localhost", host, 1),
        "--access-mode=restricted",
    ]


@pytest.mark.parametrize(
    ("conninfo", "fragments"),
    [
        # An apostrophe is an ordinary character in a URI query value
        ("postgresql://db.example.com/app?password='s3cr3t", ["s3cr3t"]),
        ("postgresql://db.example.com/app?password=foo'bar", ["foo", "bar"]),
        # Backslash escapes in an unquoted key=value password
        ("host=db.example.com password=first\\ second dbname=app", ["first", "second"]),
        ("host=db.example.com password=it\\'s dbname=app", ["it\\'s", "it's"]),
        ("host=db.example.com password='quo ted\\' pw' dbname=app", ["quo", "ted", "pw'"]),
        # Unencoded @ in a URI password
        ("postgresql://u:pct%40enc0ded@db.example.com/app", ["enc0ded"]),
    ],
)
def test_entrypoint_hides_awkward_passwords(tmp_path: Path, conninfo: str, fragments: list[str]) -> None:
    """No fragment of the password is printed, however it is quoted or escaped."""
    proc, record = _run(tmp_path, True, [conninfo], "")

    _assert_no_passwords(proc, *fragments)
    assert "host=db.example.com" in proc.stderr
    assert "dbname=app" in proc.stderr
    assert "password=****" in proc.stderr
    assert record["argv"] == [conninfo]


@pytest.mark.parametrize(
    "conninfo",
    [
        f"postgresql://u:{PASSWORDS['arg']}@[::1/db",
        # An unencoded @ in the password: libpq would put the remainder in the host
        f"postgresql://u:p@{PASSWORDS['arg']}@db.example.com/app",
    ],
)
def test_entrypoint_hides_unparseable_connection_string(tmp_path: Path, conninfo: str) -> None:
    proc, record = _run(tmp_path, True, [conninfo], "")

    _assert_no_passwords(proc)
    assert "<connection string hidden>" in proc.stderr
    assert record["argv"] == [conninfo]


def test_entrypoint_hides_connection_string_when_python_is_unavailable(tmp_path: Path) -> None:
    env_uri = f"postgresql://envuser:{PASSWORDS['env']}@localhost:5432/envdb"
    arg_uri = f"postgresql://u:{PASSWORDS['arg']}@db.example.com/app"
    proc, record = _run(tmp_path, True, [arg_uri], env_uri, no_python=True)

    _assert_no_passwords(proc)
    assert "<connection string hidden>" in proc.stderr
    assert "envuser" not in proc.stderr
    assert record["argv"] == [arg_uri]


def test_entrypoint_describes_uri_without_password(tmp_path: Path) -> None:
    env_uri = "postgresql://nopass@localhost/db"
    proc, record = _run(tmp_path, True, [], env_uri)

    assert "Found localhost in DATABASE_URI: <connection: host=localhost dbname=db user=nopass>" in proc.stderr
    assert "****" not in proc.stderr
    assert record["database_uri"] == "postgresql://nopass@host.docker.internal/db"

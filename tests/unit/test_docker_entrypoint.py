"""Tests that docker-entrypoint.sh never prints database passwords.

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


def _run(tmp_path: Path, ping_succeeds: bool, args: list[str], database_uri: str) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_ping(bin_dir, ping_succeeds)
    recorder = tmp_path / "recorder.py"
    recorder.write_text(RECORDER)
    record_file = tmp_path / "record.json"
    assert BASH is not None

    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
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


def _assert_no_passwords(proc: subprocess.CompletedProcess[str]) -> None:
    output = proc.stdout + proc.stderr
    for password in PASSWORDS.values():
        assert password not in output
    assert "****" in proc.stderr


@pytest.mark.parametrize("ping_succeeds", [True, False], ids=["docker-host-found", "docker-host-unknown"])
def test_entrypoint_masks_passwords_but_passes_real_values(tmp_path: Path, ping_succeeds: bool) -> None:
    env_uri = f"postgresql://envuser:{PASSWORDS['env']}@localhost:5432/envdb"
    arg_uri = f"postgresql://arguser:{PASSWORDS['arg']}@localhost/argdb"
    kv_arg = f"host=localhost password={PASSWORDS['kv']} dbname=kvdb"
    query_uri = f"postgresql://localhost/qdb?user=q&password={PASSWORDS['query']}&sslmode=disable"
    args = [arg_uri, kv_arg, query_uri, "--access-mode=restricted"]

    proc, record = _run(tmp_path, ping_succeeds, args, env_uri)

    _assert_no_passwords(proc)

    # Diagnostics are kept: the rewrite is reported, with the host visible
    assert "Found localhost in DATABASE_URI: postgresql://envuser:****@localhost:5432/envdb" in proc.stderr
    assert "Found localhost in database connection: postgresql://arguser:****@localhost/argdb" in proc.stderr
    assert "password=****" in proc.stderr
    assert "--access-mode=restricted" in proc.stderr

    if ping_succeeds:
        host = "host.docker.internal"
        assert f"Remapping: postgresql://envuser:****@localhost:5432/envdb --> postgresql://envuser:****@{host}:5432/envdb" in proc.stderr
        assert f"Remapping: postgresql://arguser:****@localhost/argdb --> postgresql://arguser:****@{host}/argdb" in proc.stderr
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


def test_entrypoint_masks_uri_without_localhost(tmp_path: Path) -> None:
    """URIs that are not rewritten still appear in "Executing command" and must be masked."""
    arg_uri = f"postgresql://u:{PASSWORDS['arg']}@db.example.com/app"
    proc, record = _run(tmp_path, True, [arg_uri], "")

    _assert_no_passwords(proc)
    assert "postgresql://u:****@db.example.com/app" in proc.stderr
    assert record["argv"] == [arg_uri]


def test_entrypoint_leaves_uri_without_password_unchanged(tmp_path: Path) -> None:
    env_uri = "postgresql://nopass@localhost/db"
    proc, record = _run(tmp_path, True, [], env_uri)

    assert "Found localhost in DATABASE_URI: postgresql://nopass@localhost/db" in proc.stderr
    assert "****" not in proc.stderr
    assert record["database_uri"] == "postgresql://nopass@host.docker.internal/db"

"""Unit tests for RDS IAM auth support."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from postgres_mcp.sql.rds_iam import DEFAULT_RDS_PORT
from postgres_mcp.sql.rds_iam import DEFAULT_SSLMODE
from postgres_mcp.sql.rds_iam import RdsIamAsyncConnectionPool
from postgres_mcp.sql.rds_iam import RdsIamConfig
from postgres_mcp.sql.rds_iam import parse_database_uri_for_iam

# ----- parse_database_uri_for_iam -----


def test_parse_url_form():
    cfg = parse_database_uri_for_iam(
        "postgresql://alice@db.example.com:5433/appdb?sslmode=require",
        region="us-west-2",
        aws_profile="dev",
    )
    assert cfg.host == "db.example.com"
    assert cfg.port == 5433
    assert cfg.user == "alice"
    assert cfg.dbname == "appdb"
    assert cfg.region == "us-west-2"
    assert cfg.sslmode == "require"
    assert cfg.aws_profile == "dev"
    assert cfg.extra_params == {}


def test_parse_keyword_form():
    cfg = parse_database_uri_for_iam(
        "host=db.example.com port=5432 user=alice dbname=appdb",
        region="us-east-1",
    )
    assert cfg.host == "db.example.com"
    assert cfg.port == 5432
    assert cfg.user == "alice"
    assert cfg.dbname == "appdb"
    assert cfg.region == "us-east-1"
    assert cfg.aws_profile is None


def test_parse_drops_password():
    """Any password in the URI must be discarded — IAM auth regenerates it."""
    cfg = parse_database_uri_for_iam(
        "postgresql://alice:stale-password@db.example.com/appdb",
        region="us-west-2",
    )
    assert "password" not in cfg.base_connect_kwargs()


def test_parse_default_port():
    cfg = parse_database_uri_for_iam(
        "postgresql://alice@db.example.com/appdb",
        region="us-west-2",
    )
    assert cfg.port == DEFAULT_RDS_PORT


def test_parse_default_sslmode():
    cfg = parse_database_uri_for_iam(
        "postgresql://alice@db.example.com/appdb",
        region="us-west-2",
    )
    assert cfg.sslmode == DEFAULT_SSLMODE


def test_parse_preserves_extra_params():
    cfg = parse_database_uri_for_iam(
        "postgresql://alice@db.example.com/appdb?application_name=mcp&connect_timeout=5",
        region="us-west-2",
    )
    assert cfg.extra_params == {"application_name": "mcp", "connect_timeout": "5"}


@pytest.mark.parametrize(
    "uri,missing",
    [
        ("postgresql://db.example.com/appdb", "user"),
        ("postgresql://alice@/appdb", "host"),
        ("postgresql://alice@db.example.com/", "dbname"),
    ],
)
def test_parse_missing_fields(uri: str, missing: str):
    with pytest.raises(ValueError, match=missing):
        parse_database_uri_for_iam(uri, region="us-west-2")


def test_parse_bad_port():
    with pytest.raises(ValueError, match="Invalid port"):
        parse_database_uri_for_iam(
            "host=db.example.com port=notaport user=alice dbname=appdb",
            region="us-west-2",
        )


# ----- RdsIamConfig.generate_token -----


def test_generate_token_uses_session_args():
    with patch("boto3.Session") as mock_session_cls:
        mock_client = MagicMock()
        mock_client.generate_db_auth_token.return_value = "FRESH-TOKEN"
        mock_session_cls.return_value.client.return_value = mock_client

        cfg = RdsIamConfig(
            host="db.example.com",
            port=5432,
            user="alice",
            dbname="appdb",
            region="us-west-2",
            aws_profile="dev",
        )
        token = cfg.generate_token()

        assert token == "FRESH-TOKEN"
        mock_session_cls.assert_called_once_with(profile_name="dev", region_name="us-west-2")
        mock_session_cls.return_value.client.assert_called_once_with("rds")
        mock_client.generate_db_auth_token.assert_called_once_with(
            DBHostname="db.example.com",
            Port=5432,
            DBUsername="alice",
            Region="us-west-2",
        )


def test_generate_token_no_profile_passes_none():
    with patch("boto3.Session") as mock_session_cls:
        mock_session_cls.return_value.client.return_value.generate_db_auth_token.return_value = "T"
        cfg = RdsIamConfig(
            host="h",
            port=5432,
            user="u",
            dbname="d",
            region="us-west-2",
        )
        cfg.generate_token()
        mock_session_cls.assert_called_once_with(profile_name=None, region_name="us-west-2")


# ----- RdsIamConfig.base_connect_kwargs -----


def test_base_connect_kwargs_no_password():
    cfg = RdsIamConfig(host="h", port=5432, user="u", dbname="d", region="us-west-2")
    kwargs = cfg.base_connect_kwargs()
    assert "password" not in kwargs
    assert kwargs == {"host": "h", "port": 5432, "user": "u", "dbname": "d", "sslmode": "require"}


def test_base_connect_kwargs_merges_extras():
    cfg = RdsIamConfig(
        host="h",
        port=5432,
        user="u",
        dbname="d",
        region="us-west-2",
        extra_params={"application_name": "mcp"},
    )
    assert cfg.base_connect_kwargs()["application_name"] == "mcp"


# ----- RdsIamAsyncConnectionPool -----


@pytest.fixture
def iam_config():
    return RdsIamConfig(
        host="db.example.com",
        port=5432,
        user="alice",
        dbname="appdb",
        region="us-west-2",
    )


def test_pool_init_seeds_initial_token(iam_config: RdsIamConfig):
    with patch.object(RdsIamConfig, "generate_token", return_value="INIT-TOKEN") as gen:
        pool = RdsIamAsyncConnectionPool(iam_config=iam_config, open=False, min_size=0, max_size=1)
        try:
            assert pool.kwargs["password"] == "INIT-TOKEN"
            assert pool.kwargs["host"] == "db.example.com"
            assert pool.kwargs["user"] == "alice"
            assert gen.call_count == 1
        finally:
            # No pool was opened, but be safe
            pass


@pytest.mark.asyncio
async def test_pool_refreshes_token_on_each_connect(iam_config: RdsIamConfig):
    """Every call to _connect must regenerate the token and call super()._connect."""
    tokens = iter(["T1", "T2", "T3"])
    with patch.object(RdsIamConfig, "generate_token", side_effect=lambda: next(tokens)):
        pool = RdsIamAsyncConnectionPool(iam_config=iam_config, open=False, min_size=0, max_size=1)
        # Initial token was "T1" (consumed in __init__)
        assert pool.kwargs["password"] == "T1"

        with patch(
            "psycopg_pool.AsyncConnectionPool._connect",
            new=AsyncMock(return_value=MagicMock(name="fake_conn")),
        ) as super_connect:
            await pool._connect()
            assert pool.kwargs["password"] == "T2"
            assert super_connect.await_count == 1

            await pool._connect()
            assert pool.kwargs["password"] == "T3"
            assert super_connect.await_count == 2

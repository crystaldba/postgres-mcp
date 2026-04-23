"""RDS IAM authentication support for the psycopg connection pool.

RDS IAM auth tokens are short-lived (15 minutes). A static token injected at
startup breaks the server once the token expires. This module transparently
regenerates a fresh token every time the pool opens a new physical connection,
so callers can treat an IAM-authenticated pool the same as a password pool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Dict
from typing import Optional

from psycopg import AsyncConnection
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

DEFAULT_RDS_PORT = 5432
DEFAULT_SSLMODE = "require"


@dataclass(frozen=True)
class RdsIamConfig:
    """Connection parameters for an RDS IAM-authenticated database.

    Password is intentionally absent — it's generated per-connection by
    `generate_token()` and has a 15-minute AWS-enforced TTL.
    """

    host: str
    port: int
    user: str
    dbname: str
    region: str
    sslmode: str = DEFAULT_SSLMODE
    aws_profile: Optional[str] = None
    extra_params: Dict[str, str] = field(default_factory=dict)

    def generate_token(self) -> str:
        """Return a fresh RDS IAM auth token valid for 15 minutes."""
        import boto3  # lazy import so non-IAM users don't pay the cost

        session = boto3.Session(profile_name=self.aws_profile, region_name=self.region)
        client = session.client("rds")
        return client.generate_db_auth_token(
            DBHostname=self.host,
            Port=self.port,
            DBUsername=self.user,
            Region=self.region,
        )

    def base_connect_kwargs(self) -> Dict[str, Any]:
        """Return psycopg connect kwargs with everything except the password."""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "dbname": self.dbname,
            "sslmode": self.sslmode,
            **self.extra_params,
        }


def parse_database_uri_for_iam(
    database_uri: str,
    region: str,
    aws_profile: Optional[str] = None,
) -> RdsIamConfig:
    """Parse a DATABASE_URI into an RdsIamConfig, discarding any password.

    Accepts either postgres:// URLs or libpq key=value strings. The password
    field, if present, is ignored — IAM auth regenerates it per-connection.
    """
    params = conninfo_to_dict(database_uri)

    host = params.get("host")
    user = params.get("user")
    dbname = params.get("dbname")

    missing = [name for name, val in (("host", host), ("user", user), ("dbname", dbname)) if not val]
    if missing:
        raise ValueError(
            f"DATABASE_URI missing required fields for RDS IAM auth: {', '.join(missing)}. "
            f"Expected format: postgresql://<user>@<host>:<port>/<dbname>"
        )

    port_value = params.get("port", DEFAULT_RDS_PORT)
    try:
        port = int(port_value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise ValueError(f"Invalid port in DATABASE_URI: {port_value!r}") from e

    sslmode = params.get("sslmode", DEFAULT_SSLMODE)

    reserved = {"host", "hostaddr", "port", "user", "dbname", "password", "sslmode"}
    extra_params = {k: str(v) for k, v in params.items() if k not in reserved}

    return RdsIamConfig(
        host=str(host),
        port=port,
        user=str(user),
        dbname=str(dbname),
        region=region,
        sslmode=str(sslmode),
        aws_profile=aws_profile,
        extra_params=extra_params,
    )


class RdsIamAsyncConnectionPool(AsyncConnectionPool[AsyncConnection[TupleRow]]):
    """AsyncConnectionPool that refreshes the RDS IAM token on every new
    physical connection.

    Existing pooled connections stay valid for their natural lifetime — the
    15-minute token TTL only governs connection *establishment*, not ongoing
    sessions. Each new connect pulls a fresh token from AWS transparently.
    """

    def __init__(self, iam_config: RdsIamConfig, **pool_kwargs: Any) -> None:
        self._iam_config = iam_config
        # Pass connection params via `kwargs` so we can mutate the password
        # atomically (single dict key update) on each _connect. Using conninfo
        # would force us to rebuild the whole string on every connect.
        conn_kwargs = iam_config.base_connect_kwargs()
        conn_kwargs["password"] = iam_config.generate_token()
        super().__init__(conninfo="", kwargs=conn_kwargs, **pool_kwargs)

    async def _connect(self, timeout: Optional[float] = None):  # type: ignore[override]
        self.kwargs["password"] = self._iam_config.generate_token()  # type: ignore[index]
        logger.debug("Refreshed RDS IAM token for new connection to %s", self._iam_config.host)
        return await super()._connect(timeout)

"""ClickHouse connection and query execution."""

import os
from typing import Any, Optional

import structlog
from clickhouse_driver import Client

log = structlog.get_logger(__name__)

_client_instance: Optional["ClickHouseClient"] = None


class ClickHouseClient:
    """Wraps clickhouse-driver with convenience methods."""

    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
    ) -> None:
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self._client: Optional[Client] = None

    def _get_or_create_client(self) -> Client:
        if self._client is None:
            self._client = Client(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password,
                settings={
                    "use_numpy": False,
                    "connect_timeout": 10,
                    "send_receive_timeout": 30,
                },
            )
            log.info(
                "clickhouse_client_created",
                host=self.host,
                port=self.port,
                database=self.database,
            )
        return self._client

    def _reconnect(self) -> Client:
        """Force a fresh connection."""
        self._client = None
        return self._get_or_create_client()

    def execute(self, query: str, params: Optional[Any] = None) -> Any:
        """Execute a query (DDL / INSERT / UPDATE)."""
        try:
            client = self._get_or_create_client()
            result = client.execute(query, params or [])
            return result
        except Exception as exc:
            log.warning("clickhouse_execute_error", query=query[:120], error=str(exc))
            # Attempt reconnect once
            try:
                client = self._reconnect()
                return client.execute(query, params or [])
            except Exception as retry_exc:
                log.error(
                    "clickhouse_execute_retry_failed",
                    query=query[:120],
                    error=str(retry_exc),
                )
                raise

    def execute_many(self, query: str, data: list) -> Any:
        """Bulk insert using clickhouse-driver's list-of-tuples form."""
        if not data:
            return None
        try:
            client = self._get_or_create_client()
            return client.execute(query, data)
        except Exception as exc:
            log.warning(
                "clickhouse_execute_many_error", query=query[:120], error=str(exc)
            )
            try:
                client = self._reconnect()
                return client.execute(query, data)
            except Exception as retry_exc:
                log.error(
                    "clickhouse_execute_many_retry_failed",
                    query=query[:120],
                    error=str(retry_exc),
                )
                raise

    def fetch_one(self, query: str, params: Optional[Any] = None) -> Optional[dict]:
        """Return the first row as a dict, or None."""
        try:
            client = self._get_or_create_client()
            result = client.execute(query, params or [], with_column_types=True)
            rows, columns = result
            if not rows:
                return None
            col_names = [c[0] for c in columns]
            return dict(zip(col_names, rows[0]))
        except Exception as exc:
            log.error("clickhouse_fetch_one_error", query=query[:120], error=str(exc))
            raise

    def fetch_all(self, query: str, params: Optional[Any] = None) -> list[dict]:
        """Return all rows as a list of dicts."""
        try:
            client = self._get_or_create_client()
            result = client.execute(query, params or [], with_column_types=True)
            rows, columns = result
            col_names = [c[0] for c in columns]
            return [dict(zip(col_names, row)) for row in rows]
        except Exception as exc:
            log.error("clickhouse_fetch_all_error", query=query[:120], error=str(exc))
            raise


def get_client() -> ClickHouseClient:
    """Return the module-level singleton ClickHouseClient."""
    global _client_instance
    if _client_instance is None:
        _client_instance = ClickHouseClient(
            host=os.getenv("CLICKHOUSE_HOST", "localhost"),
            port=int(os.getenv("CLICKHOUSE_PORT", "9000")),
            database=os.getenv("CLICKHOUSE_DB", "otel"),
            user=os.getenv("CLICKHOUSE_USER", "default"),
            password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        )
    return _client_instance

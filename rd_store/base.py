"""Base Redis config, client factory, and shared service class."""

import os
from dataclasses import dataclass, field, replace
from typing import Any, Self


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


@dataclass(slots=True)
class RDConfig:
    """Connection settings for a Redis server."""

    host: str = "localhost"
    port: int = 6379
    password: str | None = None
    db: int = 0
    username: str | None = None
    use_ssl: bool = False
    decode_responses: bool = True
    socket_timeout: float | None = None
    socket_connect_timeout: float | None = None
    max_connections: int | None = None
    health_check_interval: int = 0
    extra_client_kwargs: dict[str, Any] = field(default_factory=dict)

    def to_client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "db": self.db,
            "decode_responses": self.decode_responses,
            "ssl": self.use_ssl,
            "health_check_interval": self.health_check_interval,
        }

        if self.password is not None:
            kwargs["password"] = self.password
        if self.username is not None:
            kwargs["username"] = self.username
        if self.socket_timeout is not None:
            kwargs["socket_timeout"] = self.socket_timeout
        if self.socket_connect_timeout is not None:
            kwargs["socket_connect_timeout"] = self.socket_connect_timeout
        if self.max_connections is not None:
            kwargs["max_connections"] = self.max_connections

        kwargs.update(self.extra_client_kwargs)
        return kwargs

    @classmethod
    def from_env(cls, **overrides: Any) -> Self:
        values: dict[str, Any] = {}

        host = os.getenv("REDIS_HOST")
        if host:
            values["host"] = host

        port = os.getenv("REDIS_PORT")
        if port:
            values["port"] = int(port)

        password = os.getenv("REDIS_PASSWORD")
        if password is not None:
            values["password"] = password or None

        username = os.getenv("REDIS_USERNAME")
        if username is not None:
            values["username"] = username or None

        db = os.getenv("REDIS_DB")
        if db:
            values["db"] = int(db)

        use_ssl = os.getenv("REDIS_USE_SSL")
        if use_ssl is not None:
            values["use_ssl"] = _parse_bool(use_ssl)

        decode_responses = os.getenv("REDIS_DECODE_RESPONSES")
        if decode_responses is not None:
            values["decode_responses"] = _parse_bool(decode_responses)

        socket_timeout = os.getenv("REDIS_SOCKET_TIMEOUT")
        if socket_timeout:
            values["socket_timeout"] = float(socket_timeout)

        socket_connect_timeout = os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT")
        if socket_connect_timeout:
            values["socket_connect_timeout"] = float(socket_connect_timeout)

        max_connections = os.getenv("REDIS_MAX_CONNECTIONS")
        if max_connections:
            values["max_connections"] = int(max_connections)

        health_check_interval = os.getenv("REDIS_HEALTH_CHECK_INTERVAL")
        if health_check_interval:
            values["health_check_interval"] = int(health_check_interval)

        values.update(overrides)
        return cls(**values)


def load_config(**overrides: Any) -> RDConfig:
    """Load connection settings from the environment."""

    return RDConfig.from_env(**overrides)


def _redis_class() -> type[Any]:
    from redis import Redis

    return Redis


def create_client(
    config: RDConfig | None = None,
    **overrides: Any,
) -> Any:
    """Create and return a configured Redis client."""

    if config is None:
        config = load_config(**overrides)
    elif overrides:
        config = replace(config, **overrides)

    return _redis_class()(**config.to_client_kwargs())


class RDBase:
    """Base service that owns a Redis client and an optional key namespace."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        config: RDConfig | None = None,
        namespace: str | None = None,
        **client_overrides: Any,
    ) -> None:
        self.namespace = namespace

        if client is None:
            if config is None:
                self.config = load_config(**client_overrides)
            elif client_overrides:
                self.config = replace(config, **client_overrides)
            else:
                self.config = config

            self.client = create_client(config=self.config)
        else:
            self.client = client
            self.config = config

    def use_namespace(self, namespace: str | None) -> Self:
        """Set the key namespace prefix and return the service for chaining."""

        self.namespace = namespace
        return self

    def _resolve_key(self, key: str) -> str:
        if self.namespace:
            return f"{self.namespace}:{key}"
        return key

    def delete(self, *keys: str) -> int:
        resolved = [self._resolve_key(k) for k in keys]
        return self.client.delete(*resolved)

    def exists(self, *keys: str) -> int:
        resolved = [self._resolve_key(k) for k in keys]
        return self.client.exists(*resolved)

    def expire(self, key: str, seconds: int) -> bool:
        return self.client.expire(self._resolve_key(key), seconds)

    def ttl(self, key: str) -> int:
        return self.client.ttl(self._resolve_key(key))

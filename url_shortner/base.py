"""Base configs, client factories, and shared service classes for the URL shortener."""

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
class MongoConfig:
    """Connection settings for the MongoDB instance backing the URL store."""

    host: str = "localhost"
    port: int = 27017
    user: str | None = None
    password: str | None = None
    database: str = "url_shortner"
    collection: str = "url_mappings"
    auth_source: str | None = None
    timeout_ms: int = 5000
    max_pool_size: int = 50
    extra_client_kwargs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if bool(self.user) != bool(self.password):
            raise ValueError(
                "MongoConfig requires both user and password when authentication "
                "is enabled."
            )

    @property
    def uri(self) -> str:
        if self.user and self.password:
            credentials = f"{self.user}:{self.password}@"
        else:
            credentials = ""
        return f"mongodb://{credentials}{self.host}:{self.port}"

    def to_client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self.uri,
            "serverSelectionTimeoutMS": self.timeout_ms,
            "maxPoolSize": self.max_pool_size,
        }
        if self.auth_source:
            kwargs["authSource"] = self.auth_source
        kwargs.update(self.extra_client_kwargs)
        return kwargs

    @classmethod
    def from_env(cls, **overrides: Any) -> Self:
        values: dict[str, Any] = {}

        host = os.getenv("MONGODB_HOST")
        if host:
            values["host"] = host

        port = os.getenv("MONGODB_PORT")
        if port:
            values["port"] = int(port)

        user = os.getenv("MONGODB_USER")
        if user is not None:
            values["user"] = user or None

        password = os.getenv("MONGODB_PASSWORD")
        if password is not None:
            values["password"] = password or None

        database = os.getenv("MONGODB_DATABASE")
        if database:
            values["database"] = database

        collection = os.getenv("MONGODB_COLLECTION")
        if collection:
            values["collection"] = collection

        auth_source = os.getenv("MONGODB_AUTH_SOURCE")
        if auth_source:
            values["auth_source"] = auth_source

        timeout_ms = os.getenv("MONGODB_TIMEOUT_MS")
        if timeout_ms:
            values["timeout_ms"] = int(timeout_ms)

        max_pool_size = os.getenv("MONGODB_MAX_POOL_SIZE")
        if max_pool_size:
            values["max_pool_size"] = int(max_pool_size)

        values.update(overrides)
        return cls(**values)


@dataclass(slots=True)
class RedisConfig:
    """Connection settings for the Redis cache."""

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None
    ssl: bool = False
    socket_timeout: float = 2.0
    default_ttl: int = 86400
    extra_client_kwargs: dict[str, Any] = field(default_factory=dict)

    def to_client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "db": self.db,
            "ssl": self.ssl,
            "socket_timeout": self.socket_timeout,
            "decode_responses": True,
        }
        if self.password:
            kwargs["password"] = self.password
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

        db = os.getenv("REDIS_DB")
        if db:
            values["db"] = int(db)

        password = os.getenv("REDIS_PASSWORD")
        if password is not None:
            values["password"] = password or None

        ssl = os.getenv("REDIS_SSL")
        if ssl is not None:
            values["ssl"] = _parse_bool(ssl)

        socket_timeout = os.getenv("REDIS_TIMEOUT")
        if socket_timeout:
            values["socket_timeout"] = float(socket_timeout)

        default_ttl = os.getenv("REDIS_TTL")
        if default_ttl:
            values["default_ttl"] = int(default_ttl)

        values.update(overrides)
        return cls(**values)


def load_mongo_config(**overrides: Any) -> MongoConfig:
    return MongoConfig.from_env(**overrides)


def load_redis_config(**overrides: Any) -> RedisConfig:
    return RedisConfig.from_env(**overrides)


def _mongo_client_class() -> type[Any]:
    from pymongo import MongoClient

    return MongoClient


def _redis_client_class() -> type[Any]:
    from redis import Redis

    return Redis


def create_mongo_client(
    config: MongoConfig | None = None,
    **overrides: Any,
) -> Any:
    """Create and return a configured ``MongoClient``."""

    if config is None:
        config = load_mongo_config(**overrides)
    elif overrides:
        config = replace(config, **overrides)

    return _mongo_client_class()(**config.to_client_kwargs())


def create_redis_client(
    config: RedisConfig | None = None,
    **overrides: Any,
) -> Any:
    """Create and return a configured Redis client."""

    if config is None:
        config = load_redis_config(**overrides)
    elif overrides:
        config = replace(config, **overrides)

    return _redis_client_class()(**config.to_client_kwargs())


class MongoBase:
    """Base service that owns a MongoDB client plus default database/collection."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        config: MongoConfig | None = None,
        database: str | None = None,
        collection: str | None = None,
        **client_overrides: Any,
    ) -> None:
        if client is not None and client_overrides:
            raise ValueError(
                "Client overrides cannot be used when an existing client instance "
                "is supplied."
            )

        if client is None:
            if config is None:
                self.config = load_mongo_config(**client_overrides)
            elif client_overrides:
                self.config = replace(config, **client_overrides)
            else:
                self.config = config

            self.client = create_mongo_client(config=self.config)
        else:
            self.client = client
            self.config = config

        self.default_database = database or (self.config.database if self.config else None)
        self.default_collection = collection or (
            self.config.collection if self.config else None
        )

    def use_database(self, database: str) -> Self:
        self.default_database = database
        return self

    def use_collection(self, collection: str) -> Self:
        self.default_collection = collection
        return self

    def _resolve_database(self, database: str | None = None) -> str:
        resolved = database or self.default_database
        if resolved is None:
            raise ValueError("A database name is required for this operation.")
        return resolved

    def _resolve_collection(self, collection: str | None = None) -> str:
        resolved = collection or self.default_collection
        if resolved is None:
            raise ValueError("A collection name is required for this operation.")
        return resolved

    def _coll(
        self,
        *,
        database: str | None = None,
        collection: str | None = None,
    ) -> Any:
        db_name = self._resolve_database(database)
        coll_name = self._resolve_collection(collection)
        return self.client[db_name][coll_name]


class RedisBase:
    """Base service that owns a Redis client."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        config: RedisConfig | None = None,
        **client_overrides: Any,
    ) -> None:
        if client is not None and client_overrides:
            raise ValueError(
                "Client overrides cannot be used when an existing client instance "
                "is supplied."
            )

        if client is None:
            if config is None:
                self.config = load_redis_config(**client_overrides)
            elif client_overrides:
                self.config = replace(config, **client_overrides)
            else:
                self.config = config

            self.client = create_redis_client(config=self.config)
        else:
            self.client = client
            self.config = config

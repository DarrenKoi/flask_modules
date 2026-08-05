"""Base MinIO config, client factory, and shared service class."""

import importlib
import os
import time
from dataclasses import dataclass, field, replace
from typing import Any, Self

_CONNECTION_ATTRS = {
    "endpoint": "ENDPOINT",
    "access_key": "ACCESS_KEY",
    "secret_key": "SECRET_KEY",
    "secure": "SECURE",
    "region": "REGION",
    "cert_check": "CERT_CHECK",
}

_OBJECT_ATTRS = {
    "bucket": "BUCKET",
    "prefix": "PREFIX",
}


def _elapsed_ms(started: float) -> float:
    """Milliseconds since a ``time.perf_counter()`` mark, rounded for logging."""

    return round((time.perf_counter() - started) * 1000, 2)


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _module_values(attr_map: dict[str, str]) -> dict[str, Any]:
    """Return non-None constants defined in ``minio_handler.minio_config``.

    Missing module or missing attributes are silently ignored so the package
    works on a fresh clone where the gitignored config file is absent.
    """

    module_name = f"{__package__}.minio_config"
    try:
        mod = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        return {}

    values: dict[str, Any] = {}
    for key, attr in attr_map.items():
        if not hasattr(mod, attr):
            continue
        value = getattr(mod, attr)
        if value is None:
            continue
        values[key] = value
    return values


@dataclass(slots=True)
class ConnectionStatus:
    """Outcome of a connection check against the MinIO endpoint.

    ``ok`` answers the only question most callers have, so the object is
    truthy/falsy directly. ``detail`` carries whatever the probe learned (the
    bucket it hit and whether that bucket exists, or the bucket listing) and
    ``error`` holds the formatted exception when the probe failed, so a
    startup gate can log something actionable instead of a bare False.
    """

    ok: bool
    elapsed_ms: float
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass(slots=True)
class MinioConfig:
    """Connection settings for a MinIO / S3-compatible endpoint."""

    endpoint: str = "localhost:9000"
    access_key: str | None = None
    secret_key: str | None = None
    secure: bool = False
    region: str | None = None
    cert_check: bool = True
    extra_client_kwargs: dict[str, Any] = field(default_factory=dict)

    def to_client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "endpoint": self.endpoint,
            "access_key": self.access_key,
            "secret_key": self.secret_key,
            "secure": self.secure,
            "cert_check": self.cert_check,
        }

        if self.region:
            kwargs["region"] = self.region

        kwargs.update(self.extra_client_kwargs)
        return kwargs

    @classmethod
    def from_env(cls, **overrides: Any) -> Self:
        values: dict[str, Any] = _module_values(_CONNECTION_ATTRS)

        endpoint = os.getenv("MINIO_ENDPOINT")
        if endpoint:
            values["endpoint"] = endpoint

        access_key = os.getenv("MINIO_ACCESS_KEY")
        if access_key is not None:
            values["access_key"] = access_key or None

        secret_key = os.getenv("MINIO_SECRET_KEY")
        if secret_key is not None:
            values["secret_key"] = secret_key or None

        secure = os.getenv("MINIO_SECURE")
        if secure is not None:
            values["secure"] = _parse_bool(secure)

        region = os.getenv("MINIO_REGION")
        if region:
            values["region"] = region

        cert_check = os.getenv("MINIO_CERT_CHECK")
        if cert_check is not None:
            values["cert_check"] = _parse_bool(cert_check)

        values.update(overrides)
        return cls(**values)


def load_config(**overrides: Any) -> MinioConfig:
    """Load MinIO connection settings from the environment."""

    return MinioConfig.from_env(**overrides)


def _minio_class() -> type[Any]:
    from minio import Minio

    return Minio


def create_client(
    config: MinioConfig | None = None,
    **overrides: Any,
) -> Any:
    """Create and return a configured MinIO client."""

    if config is None:
        config = load_config(**overrides)
    elif overrides:
        config = replace(config, **overrides)

    return _minio_class()(**config.to_client_kwargs())


class MinioBase:
    """Base service that owns a MinIO client, default bucket, and key prefix."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        config: MinioConfig | None = None,
        bucket: str | None = None,
        prefix: str | None = None,
        **client_overrides: Any,
    ) -> None:
        module_defaults = _module_values(_OBJECT_ATTRS)
        resolved_bucket = bucket if bucket is not None else module_defaults.get("bucket")
        resolved_prefix = prefix if prefix is not None else module_defaults.get("prefix")

        self.default_bucket = resolved_bucket
        self.default_prefix = resolved_prefix.strip("/") if resolved_prefix else None

        if client is not None and client_overrides:
            raise ValueError(
                "Client overrides cannot be used when an existing client instance "
                "is supplied."
            )

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

    def ping(self, bucket: str | None = None) -> bool:
        """Return True when the endpoint answers and the credentials work.

        Never raises: an unreachable endpoint, wrong keys, or a TLS failure
        all come back as False, so this is safe to call from a startup gate or
        a health endpoint. A bucket that simply does not exist still counts as
        connected — use ``check_connection`` to tell the two apart.
        """

        return self.check_connection(bucket).ok

    def check_connection(self, bucket: str | None = None) -> ConnectionStatus:
        """Probe the endpoint and report why it failed when it does.

        MinIO has no ping, so this hits ``bucket_exists`` on the resolved
        bucket — ``bucket``, else the default bucket, which itself comes from
        ``minio_config.BUCKET`` when the service was built without one. It is
        the cheapest round trip that also exercises the credentials, and the
        answer lands in ``detail["bucket_exists"]``. No ``list_buckets``
        fallback: an account scoped to one bucket cannot list.
        """

        started = time.perf_counter()
        try:
            target = self._resolve_bucket(bucket)
            detail: dict[str, Any] = {
                "bucket": target,
                "bucket_exists": bool(self.client.bucket_exists(target)),
            }
        except Exception as exc:
            return ConnectionStatus(
                ok=False,
                elapsed_ms=_elapsed_ms(started),
                error=f"{type(exc).__name__}: {exc}",
            )

        return ConnectionStatus(ok=True, elapsed_ms=_elapsed_ms(started), detail=detail)

    def use_bucket(self, bucket: str) -> Self:
        """Set the default bucket and return the service for chaining."""

        self.default_bucket = bucket
        return self

    def use_prefix(self, prefix: str | None) -> Self:
        """Set the default key prefix and return the service for chaining."""

        self.default_prefix = prefix.strip("/") if prefix else None
        return self

    def _resolve_bucket(self, bucket: str | None = None) -> str:
        resolved = bucket or self.default_bucket
        if resolved is None:
            raise ValueError("A bucket name is required for this operation.")
        return resolved

    def _resolve_key(self, key: str, *, prefix: str | None = None) -> str:
        active_prefix = prefix if prefix is not None else self.default_prefix
        cleaned_key = key.lstrip("/")
        if not active_prefix:
            return cleaned_key
        return f"{active_prefix.strip('/')}/{cleaned_key}"

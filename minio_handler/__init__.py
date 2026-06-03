"""Class-based MinIO / S3-compatible client wrappers."""

from .base import MinioBase, MinioConfig, create_client, load_config
from .object import GetManyResult, MinioObject

__all__ = [
    "GetManyResult",
    "MinioBase",
    "MinioConfig",
    "MinioObject",
    "create_client",
    "load_config",
]

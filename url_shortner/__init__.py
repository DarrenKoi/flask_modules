"""Internal URL shortener package: Flask + MongoDB + Redis + OpenSearch analytics."""

from .analytics import ClickAnalytics
from .app import create_app
from .base import (
    MongoBase,
    MongoConfig,
    RedisBase,
    RedisConfig,
    create_mongo_client,
    create_redis_client,
    load_mongo_config,
    load_redis_config,
)
from .cache import CacheLayer
from .codegen import generate_code, is_valid_alias
from .mapping import URLMapping
from .service import AliasTakenError, ShortenerService

__all__ = [
    "AliasTakenError",
    "CacheLayer",
    "ClickAnalytics",
    "MongoBase",
    "MongoConfig",
    "RedisBase",
    "RedisConfig",
    "ShortenerService",
    "URLMapping",
    "create_app",
    "create_mongo_client",
    "create_redis_client",
    "generate_code",
    "is_valid_alias",
    "load_mongo_config",
    "load_redis_config",
]

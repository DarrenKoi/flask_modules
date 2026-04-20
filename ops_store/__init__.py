"""Purpose-specific OpenSearch service classes built on top of opensearch-py."""

from .base import OSConfig, create_client, load_config
from .document import OSDoc
from .index import OSIndex
from .logging import configure_logging, get_logger
from .search import OSSearch

__all__ = [
    "OSConfig",
    "OSDoc",
    "OSIndex",
    "OSSearch",
    "configure_logging",
    "create_client",
    "get_logger",
    "load_config",
]

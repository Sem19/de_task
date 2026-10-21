"""PostgreSQL connectivity, schema verification, and explicit migrations."""

from .connection import StorageError, connect, connection, safe_dsn_context
from .migrate import apply_migrations
from .schema import SchemaError, verify_schema

__all__ = [
    "SchemaError",
    "StorageError",
    "apply_migrations",
    "connect",
    "connection",
    "safe_dsn_context",
    "verify_schema",
]

"""PostgreSQL connectivity, schema, loading, and extraction state."""

from .connection import StorageError, connect, connection, safe_dsn_context
from .loader import LoadResult, replace_date_slice
from .lock import ExtractionLock, acquire_extraction_lock
from .migrate import apply_migrations
from .schema import SchemaError, verify_schema
from .state import ExtractState, StateConflictError, apply_snapshot, read_state

__all__ = [
    "ExtractState",
    "ExtractionLock",
    "LoadResult",
    "SchemaError",
    "StateConflictError",
    "StorageError",
    "acquire_extraction_lock",
    "apply_migrations",
    "apply_snapshot",
    "connect",
    "connection",
    "read_state",
    "replace_date_slice",
    "safe_dsn_context",
    "verify_schema",
]

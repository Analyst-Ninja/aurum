"""Error hierarchy: pipelines retry TransientError, fail loud on PermanentError."""

from src.core.errors import (
    DataSourceError,
    PermanentError,
    SchemaMismatchError,
    TransientError,
)


def test_hierarchy():
    assert issubclass(TransientError, DataSourceError)
    assert issubclass(PermanentError, DataSourceError)
    assert issubclass(SchemaMismatchError, PermanentError)
    assert not issubclass(TransientError, PermanentError)

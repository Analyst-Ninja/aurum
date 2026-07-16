"""Error hierarchy for datasources and pipelines.

Pipelines retry TransientError with backoff; PermanentError fails the run loudly.
"""


class DataSourceError(Exception):
    """Base for all datasource/pipeline errors."""


class TransientError(DataSourceError):
    """Retryable: 5xx, timeout, socket drop."""


class PermanentError(DataSourceError):
    """Not retryable: bad config, 403, schema mismatch."""


class SchemaMismatchError(PermanentError):
    """Fetched data does not conform to the declared record schema."""

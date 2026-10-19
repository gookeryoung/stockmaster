"""Domain exceptions."""

from __future__ import annotations


class StockMasterError(Exception):
    """Base class for all application errors."""


class ConfigError(StockMasterError):
    """Invalid or missing configuration."""


class DataError(StockMasterError):
    """Malformed or inconsistent data."""


class ProviderError(StockMasterError):
    """A data provider failed to satisfy a request."""


class ProviderTimeout(ProviderError):
    """A provider request exceeded its deadline."""


class ProviderUnavailable(ProviderError):
    """All configured providers are unavailable (circuit-broken or failed)."""


class SymbolNotFound(StockMasterError):
    """The requested instrument is unknown."""


class InsufficientData(StockMasterError):
    """Not enough history to satisfy the request (e.g. indicator warm-up)."""


class ModelNotTrained(StockMasterError):
    """No trained model is available for the requested task."""


class InvalidAlgoParams(StockMasterError):
    """Hyperparameters are unknown, mistyped, or outside the supported range."""

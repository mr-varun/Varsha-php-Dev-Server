"""Exception types shared across varsha."""

from __future__ import annotations


class VarshaError(Exception):
    """Base class for errors that should be shown to the user as-is."""


class ConfigError(VarshaError):
    """A config file (services.json, settings.json) is missing or invalid."""


class ServiceError(VarshaError):
    """A service action failed. `service_id` lets the GUI offer "View log"."""

    def __init__(self, message: str, service_id: str | None = None) -> None:
        super().__init__(message)
        self.service_id = service_id

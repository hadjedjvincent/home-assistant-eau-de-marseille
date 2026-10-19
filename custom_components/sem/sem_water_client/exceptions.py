"""Exceptions independent of Home Assistant and HTTP response bodies."""


class SEMError(Exception):
    """Base error for the SEM client."""


class CannotConnect(SEMError):
    """The portal is unavailable or returned an invalid protocol response."""


class InvalidAuth(SEMError):
    """The portal rejected the credentials or the authenticated session."""

class WardenError(Exception):
    """Base class for errors that map onto an HTTP response."""

    status_code = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownServiceError(WardenError):
    status_code = 404


class PortUnavailableError(WardenError):
    status_code = 409


class PoolExhaustedError(WardenError):
    status_code = 503


class UnknownProcessError(WardenError):
    status_code = 404


class NotPermittedError(WardenError):
    status_code = 403


class ProtectedProcessError(WardenError):
    status_code = 403


class StillRunningError(WardenError):
    status_code = 409


class UnknownNodeError(WardenError):
    status_code = 404


class UpdateFailedError(WardenError):
    status_code = 500


class RelayedError(WardenError):
    """An answer from another warden, handed on exactly as it came.

    The node owns the decision, so it owns the wording too. Rewriting its 409
    into the hub's own words would hide which machine refused, and why.
    """

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code

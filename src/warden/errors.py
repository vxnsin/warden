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

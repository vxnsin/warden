class PortManagerError(Exception):
    """Base class for errors that map onto an HTTP response."""

    status_code = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownServiceError(PortManagerError):
    status_code = 404


class PortUnavailableError(PortManagerError):
    status_code = 409


class PoolExhaustedError(PortManagerError):
    status_code = 503

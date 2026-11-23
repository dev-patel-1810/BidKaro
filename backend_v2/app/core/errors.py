from dataclasses import dataclass


@dataclass(slots=True)
class AppError(Exception):
    """A typed error the route layer maps to one HTTP status code.

    User-facing messages are sentences about the data, not about the code
    (CODING-GUIDE §10).
    """

    status_code: int
    detail: str


class NotFound(AppError):
    def __init__(self, detail: str = "Not found.") -> None:
        super().__init__(404, detail)


class Conflict(AppError):
    def __init__(self, detail: str = "The request conflicts with the current state.") -> None:
        super().__init__(409, detail)


class BadRequest(AppError):
    def __init__(self, detail: str = "The request is not valid.") -> None:
        super().__init__(422, detail)


class ServiceUnavailable(AppError):
    def __init__(self, detail: str = "A required service is not configured.") -> None:
        super().__init__(503, detail)


class RateLimited(AppError):
    def __init__(self, detail: str = "Too many requests. Try again later.") -> None:
        super().__init__(429, detail)

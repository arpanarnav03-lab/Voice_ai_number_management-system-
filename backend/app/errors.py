from __future__ import annotations


class AppError(Exception):
    """Raised by services; rendered as {"code", "message", ...extra} JSON."""

    def __init__(self, code: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def body(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}

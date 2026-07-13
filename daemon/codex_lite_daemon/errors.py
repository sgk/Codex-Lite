from __future__ import annotations

from fastapi import HTTPException


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def http_error(code: str, message: str, status_code: int = 400, details: dict | None = None) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"error": {"code": code, "message": message, "details": details or {}}},
    )

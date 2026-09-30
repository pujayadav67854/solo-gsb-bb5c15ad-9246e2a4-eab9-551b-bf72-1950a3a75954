"""业务错误类型：携带稳定的机器可读 code 与 HTTP 状态码。"""
from __future__ import annotations

from typing import Any


class APIError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        # 可选的机读明细（如确认时逐批不符清单），原样并入 error 对象
        self.details = details


def bad_request(code: str, message: str, details: Any | None = None) -> APIError:
    return APIError(400, code, message, details)


def not_found(code: str, message: str, details: Any | None = None) -> APIError:
    return APIError(404, code, message, details)


def conflict(code: str, message: str, details: Any | None = None) -> APIError:
    return APIError(409, code, message, details)


def unprocessable(code: str, message: str, details: Any | None = None) -> APIError:
    return APIError(422, code, message, details)

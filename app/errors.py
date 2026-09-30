"""业务错误类型：携带稳定的机器可读 code 与 HTTP 状态码。"""
from __future__ import annotations


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def bad_request(code: str, message: str) -> APIError:
    return APIError(400, code, message)


def not_found(code: str, message: str) -> APIError:
    return APIError(404, code, message)


def conflict(code: str, message: str) -> APIError:
    return APIError(409, code, message)


def unprocessable(code: str, message: str) -> APIError:
    return APIError(422, code, message)

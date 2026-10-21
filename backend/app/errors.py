"""API error format (rule R12): every error body is {"detail": "<message>", "code": "<snake_case>"}.

The frontend mirrors these codes in src/api.ts and maps them to messages and actions (07-ux §6.8).
"""

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

CODES = {
    "not_signed_in",
    "invalid_credentials",
    "csrf_invalid",
    "rate_limited",
    "forbidden",
    "not_found",
    "conflict",
    "bad_request",
    "wiki_conflict",
    "session_busy",
    "budget_exceeded",
    "llm_unavailable",
    "unsupported_file",
    "file_too_large",
    "disk_full",
    "credential_invalid",
    "password_change_required",
    "validation_error",
    "internal",
}

# Code used when a plain HTTPException is raised without one.
DEFAULT_CODES = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "not_signed_in",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "not_found",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_413_CONTENT_TOO_LARGE: "file_too_large",
    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: "unsupported_file",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "validation_error",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
}


class AppError(HTTPException):
    def __init__(self, status_code: int, code: str, detail: str, **extra: object) -> None:
        if code not in CODES:
            raise ValueError(f"Unknown error code {code!r}; add it to app.errors.CODES")
        super().__init__(status_code, detail)
        self.code = code
        self.extra = extra


def _body(detail: object, code: str, **extra: object) -> dict[str, object]:
    return {"detail": detail if isinstance(detail, str) else str(detail), "code": code, **extra}


def install(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = getattr(exc, "code", None) or DEFAULT_CODES.get(exc.status_code, f"http_{exc.status_code}")
        extra = getattr(exc, "extra", {})
        return JSONResponse(_body(exc.detail, code, **extra), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", "")} for e in exc.errors()]
        first = errors[0]["msg"] if errors else "Invalid request"
        return JSONResponse(
            _body(first, "validation_error", errors=errors), status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )

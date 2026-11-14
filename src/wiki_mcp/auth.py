"""Streamable HTTP 모드용 Bearer token 인증 미들웨어.

FastMCP의 `streamable_http_app()`이 반환하는 Starlette app 위에
`Authorization: Bearer <key>` 검증을 얹는다. stdio / MCPO 경로에는 영향 없음.
"""
from __future__ import annotations

import sys

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """`Authorization: Bearer <api_key>` 헤더 검증."""

    def __init__(self, app, api_key: str) -> None:
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                {"error": "missing or invalid Authorization header (expected 'Bearer <key>')"},
                status_code=401,
            )
        token = auth[len("Bearer "):].strip()
        # constant-time 비교로 타이밍 공격 완화
        from hmac import compare_digest

        if not compare_digest(token, self._api_key):
            return JSONResponse({"error": "invalid API key"}, status_code=401)
        return await call_next(request)


def install_auth(app, api_key: str) -> None:
    """Starlette app에 Bearer auth 미들웨어 등록.

    Args:
        app: `mcp.streamable_http_app()`이 반환한 Starlette 인스턴스
        api_key: Bearer token. 빈 문자열이면 미들웨어 미부착 (경고만 출력)

    Notes:
        Starlette은 첫 요청 전에 `add_middleware()` 호출 시에만 반영되므로
        uvicorn.run() 직전에 호출해야 한다.
    """
    if not api_key:
        print(
            "⚠️  WIKI_MCP_API_KEY 미설정 — Bearer auth 없이 실행 중. "
            "127.0.0.1 이외의 인터페이스에 바인딩하지 말 것.",
            file=sys.stderr,
        )
        return

    app.add_middleware(BearerAuthMiddleware, api_key=api_key)
    print("🔒 Bearer token 인증 활성화", file=sys.stderr)

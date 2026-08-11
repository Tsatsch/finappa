from __future__ import annotations

import secrets
from pathlib import Path

from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
    requires,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from stats.data import fetch_ledger
from stats.settings import StatsSettings


class BasicAuthBackend(AuthenticationBackend):
    def __init__(self, username: str, password: str) -> None:
        self._username = username
        self._password = password

    async def authenticate(self, conn: HTTPConnection):
        auth = conn.headers.get("Authorization")
        if auth is None:
            raise AuthenticationError("Missing credentials")
        scheme, _, credentials = auth.partition(" ")
        if scheme.lower() != "basic" or not credentials:
            raise AuthenticationError("Invalid auth scheme")
        import base64

        try:
            decoded = base64.b64decode(credentials).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise AuthenticationError("Invalid credentials encoding") from exc
        username, _, password = decoded.partition(":")
        user_ok = secrets.compare_digest(username, self._username)
        pass_ok = secrets.compare_digest(password, self._password)
        if not (user_ok and pass_ok):
            raise AuthenticationError("Invalid username or password")
        return AuthCredentials(["authenticated"]), SimpleUser(username)


def _on_auth_error(conn: HTTPConnection, exc: Exception) -> Response:
    return Response(
        "Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="finappa stats"'},
    )


def create_app(settings: StatsSettings) -> Starlette:
    static_dir = Path(__file__).resolve().parent / "static"

    @requires("authenticated")
    async def api_data(request: Request) -> JSONResponse:
        payload = fetch_ledger(settings.db_path, settings.timezone)
        return JSONResponse(payload)

    @requires("authenticated")
    async def index(request: Request) -> Response:
        index_path = static_dir / "index.html"
        return Response(
            index_path.read_text(encoding="utf-8"),
            media_type="text/html; charset=utf-8",
        )

    routes = [
        Route("/", endpoint=index),
        Route("/api/data", endpoint=api_data),
        Mount("/static", app=StaticFiles(directory=static_dir), name="static"),
    ]
    middleware = [
        Middleware(
            AuthenticationMiddleware,
            backend=BasicAuthBackend(settings.stats_username, settings.stats_password),
            on_error=_on_auth_error,
        )
    ]
    return Starlette(routes=routes, middleware=middleware)

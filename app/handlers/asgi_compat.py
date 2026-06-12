"""Proof-of-concept ASGI (Starlette) compatibility layer for baselayer handlers.

See ``doc/fastapi-migration.md`` for the full design. The idea: keep the
``class XHandler(BaseHandler): async def get(self, id): ... self.success(...)``
programming model that all ~211 SkyPortal handlers use, but run it on Starlette
+ uvicorn instead of Tornado.

This module is a **proof of concept**: it implements the request/response
adapter, method dispatch, and the core ``BaseHandler`` surface
(``get_json``/``success``/``error``/query args/``write``/``set_status``/
``set_header``). The deploy-critical pieces -- Tornado-compatible *signed
cookies* and the *python-social-auth* flow -- are stubbed and marked ``TODO``;
the PoC uses a simple stdlib-HMAC cookie so a request can round-trip end to end.

Run the smoke test (after ``uv add starlette uvicorn``)::

    python -m baselayer.app.handlers.asgi_compat
    curl -s localhost:8999/ping
    curl -s localhost:8999/echo -XPOST -d '{"hello": "world"}'
"""

from __future__ import annotations

import inspect
import json
import re

# Starlette is an optional dependency during the migration; import lazily so
# importing baselayer on a Tornado-only install does not fail.
try:
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route
    from starlette.applications import Starlette
except ImportError:  # pragma: no cover - exercised only pre-`uv sync`
    Request = Response = Route = Starlette = None  # type: ignore

# Tornado-byte-compatible signed cookies, with no tornado dependency (see
# secure_cookie.py). Verified to read/write Tornado's v2 format, so logged-in
# sessions survive the migration cutover. The fallback import lets the
# bottom-of-file smoke test run as a plain script.
try:
    from . import secure_cookie
except ImportError:  # pragma: no cover
    import secure_cookie


class _MissingArgument(Exception):
    """Raised (→ HTTP 400) when a required query argument is absent."""


_NO_DEFAULT = object()


class Handler:
    """Compat base class: the Tornado ``BaseHandler`` surface, on Starlette.

    SkyPortal/baselayer handlers subclass this and define ``get``/``post``/
    ``put``/``delete`` exactly as before. The ASGI endpoint (below) drives the
    lifecycle: ``prepare`` → method → build response → ``on_finish``.
    """

    def __init__(self, request: "Request", app=None, path_args=None):
        self._request = request
        self.app = app
        self.cfg = getattr(app, "cfg", None)
        self.path_args = path_args or []
        self.body: bytes = b""  # populated by the endpoint before dispatch

        # buffered response state, flushed into a Starlette Response at the end
        self._status = 200
        self._headers: dict[str, str] = {}
        self._chunks: list[bytes] = []

    # -- request --------------------------------------------------------- #
    @property
    def request(self):
        # Tornado handlers read self.request.{body,uri,path}; adapt Starlette's.
        return _RequestView(self._request, self.body)

    def get_json(self) -> dict:
        if not self.body:
            return {}
        try:
            data = json.loads(self.body)
        except json.JSONDecodeError:
            raise Exception(
                f"JSON decode of request body failed on {self._request.url.path}."
                " Please ensure all requests are of type application/json."
            )
        if not isinstance(data, dict):
            raise Exception("Please ensure posted data is of type application/json")
        return data

    def get_argument(self, name, default=_NO_DEFAULT):
        if name in self._request.query_params:
            return self._request.query_params[name]
        if default is _NO_DEFAULT:
            raise _MissingArgument(f"Missing required argument {name!r}")
        return default

    def get_query_argument(self, value, default=_NO_DEFAULT, type=None, **kwargs):
        if "default" in kwargs:  # tornado-compat kwarg
            default = kwargs["default"]
        arg = self.get_argument(value, default=default)
        if isinstance(default, bool):
            return str(arg).lower() in ("true", "yes", "t", "1")
        if type is not None and arg is not None and arg is not default:
            try:
                return type(arg)
            except (TypeError, ValueError):
                return default
        return arg

    # -- secure cookies (Tornado-byte-compatible; see secure_cookie.py) --- #
    def _cookie_secret(self):
        return (self.cfg or {}).get("app.secret_key", "dev-secret")

    def get_secure_cookie(self, name, max_age_days=31):
        # Returns bytes (Tornado semantics) or None.
        return secure_cookie.decode_signed_value(
            self._cookie_secret(),
            name,
            self._request.cookies.get(name),
            max_age_days=max_age_days,
        )

    def set_secure_cookie(self, name, value):
        self._set_cookies = getattr(self, "_set_cookies", {})
        self._set_cookies[name] = secure_cookie.create_signed_value(
            self._cookie_secret(), name, str(value)
        ).decode()

    @property
    def current_user(self):
        # baselayer's PSABaseHandler.get_current_user, reduced; SkyPortal extends
        # this to also accept Authorization-header tokens.
        # TODO: port the full get_current_user (User + SocialAuth uid check).
        uid = self.get_secure_cookie("user_id")
        return {"id": int(uid)} if uid else None

    # -- response -------------------------------------------------------- #
    def set_status(self, code):
        self._status = code

    def set_header(self, name, value):
        self._headers[name] = value

    def write(self, chunk):
        if isinstance(chunk, dict):
            chunk = json.dumps(chunk)
            self.set_header("Content-Type", "application/json")
        self._chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)

    def success(self, data=None, action=None, status=200, extra=None):
        if action is not None:
            self.push(action)
        self.set_header("Content-Type", "application/json")
        self.set_status(status)
        self.write(json.dumps({"status": "success", "data": data or {}, **(extra or {})}))

    def error(self, message, data=None, status=400, extra=None):
        self.set_header("Content-Type", "application/json")
        self.set_status(status)
        self.write(
            json.dumps(
                {"status": "error", "message": message, "data": data or {}, **(extra or {})}
            )
        )

    # -- realtime (already out-of-process over ZMQ) ---------------------- #
    def push(self, action, payload=None):
        # TODO: wire baselayer.app.flow.Flow().push(self.current_user["id"], ...)
        pass

    def push_all(self, action, payload=None):
        pass

    # -- lifecycle ------------------------------------------------------- #
    def prepare(self):
        """Override point; runs before the HTTP method (mirrors Tornado)."""

    def on_finish(self):
        """Per-request teardown; e.g. DBSession.remove(). Always runs."""

    def _build_response(self) -> "Response":
        resp = Response(
            content=b"".join(self._chunks),
            status_code=self._status,
            headers=self._headers,
        )
        for name, val in getattr(self, "_set_cookies", {}).items():
            resp.set_cookie(name, val, httponly=True)
        return resp


class _RequestView:
    """Minimal Tornado-``request``-shaped view over a Starlette request."""

    def __init__(self, starlette_request: "Request", body: bytes):
        self.body = body
        self.uri = str(starlette_request.url)
        self.path = starlette_request.url.path
        self.method = starlette_request.method
        self.headers = starlette_request.headers


def asgi_endpoint(handler_cls):
    """Wrap a ``Handler`` subclass as a Starlette endpoint coroutine."""

    async def endpoint(request: "Request") -> "Response":
        app = request.scope.get("app")
        path_args = list(request.path_params.values())
        handler = handler_cls(request, app=app, path_args=path_args)
        handler.body = await request.body()  # read body before (sync) dispatch
        try:
            handler.prepare()
            method = getattr(handler, request.method.lower(), None)
            if method is None:
                handler.set_status(405)
                handler.write("Method Not Allowed")
            else:
                result = method(**request.path_params)
                if inspect.isawaitable(result):
                    await result
            return handler._build_response()
        except _MissingArgument as e:
            handler.error(str(e), status=400)
            return handler._build_response()
        finally:
            handler.on_finish()

    return endpoint


def tornado_route_to_starlette(pattern: str) -> str:
    """Translate a Tornado route regex into a Starlette path template.

    ``r"/source/(?P<obj_id>[^/]+)"`` -> ``/source/{obj_id}``;
    positional ``([^/]+)`` groups become ``{arg0}``, ``{arg1}``, ...
    Anchors/trailing ``/?`` and capturing of optional ids are simplified here;
    SkyPortal's already-`: int`-annotated handlers map most cleanly.
    """
    path = pattern.rstrip("$").rstrip("/?")
    path = re.sub(r"\(\?P<(\w+)>[^)]+\)", r"{\1}", path)  # named groups
    counter = {"i": 0}

    def _pos(_m):
        name = f"arg{counter['i']}"
        counter["i"] += 1
        return "{" + name + "}"

    path = re.sub(r"\([^)]*\)", _pos, path)  # positional groups
    return path or "/"


def build_app(routes, **app_kwargs):
    """Build a Starlette app from ``[(pattern, HandlerClass), ...]`` tuples."""
    starlette_routes = [
        Route(
            tornado_route_to_starlette(pattern),
            asgi_endpoint(handler_cls),
            methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        )
        for pattern, handler_cls in routes
    ]
    return Starlette(routes=starlette_routes, **app_kwargs)


# --------------------------------------------------------------------------- #
# Smoke test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    class PingHandler(Handler):
        def get(self):
            self.success(data={"pong": True})

    class EchoHandler(Handler):
        async def post(self):
            self.success(data={"you_sent": self.get_json()})

    app = build_app([(r"/ping", PingHandler), (r"/echo", EchoHandler)])
    uvicorn.run(app, host="127.0.0.1", port=8999)

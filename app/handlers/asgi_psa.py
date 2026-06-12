"""python-social-auth on the ASGI (Starlette) stack -- the login *write* path.

Part of the Tornado -> ASGI migration (issue #381). The storage layer
(``baselayer.app.psa.TornadoStorage`` and its ``SQLAlchemy*Mixin`` models) is
framework-agnostic and reused unchanged; only the *strategy* (the
request/redirect/session adapter) is Tornado-coupled. ``StarletteStrategy``
below is a faithful port of ``TornadoStrategy`` backed by the compat
``Handler``, plus the three auth route handlers ported from
``app/handlers/auth.py``.
"""

from __future__ import annotations

import json
import urllib.parse
from functools import wraps

from social_core.actions import do_auth, do_complete, do_disconnect
from social_core.backends.utils import get_backend
from social_core.strategy import BaseStrategy
from social_core.utils import build_absolute_uri as _build_absolute_uri
from social_core.utils import get_strategy, setting_name

from .asgi_compat import Handler

DEFAULTS = {
    "STORAGE": "baselayer.app.psa.TornadoStorage",
    "STRATEGY": "baselayer.app.handlers.asgi_psa.StarletteStrategy",
}


class StarletteStrategy(BaseStrategy):
    """Port of ``baselayer.app.psa.TornadoStrategy`` for the compat Handler."""

    def __init__(self, storage, handler, tpl=None):
        self.handler = handler
        self.request = handler._request  # the Starlette request
        super().__init__(storage, tpl)

    def get_setting(self, name):
        return self.handler.settings[name]

    def request_data(self, merge=True):
        data = dict(self.request.query_params)
        body = getattr(self.handler, "body", b"") or b""
        if body:
            for key, val in urllib.parse.parse_qsl(body.decode(errors="ignore")):
                data.setdefault(key, val)
        return data

    def request_host(self):
        return self.request.headers.get("host", self.request.url.netloc)

    def request_is_secure(self):
        return self.request.url.scheme == "https"

    def request_path(self):
        return self.request.url.path

    def redirect(self, url):
        return self.handler.redirect(url)

    def html(self, content):
        self.handler.write(content)

    def session_get(self, name, default=None):
        value = self.handler.get_secure_cookie(name)
        if value:
            return json.loads(value.decode())
        return default

    def session_set(self, name, value):
        self.handler.set_secure_cookie(name, json.dumps(value))

    def session_pop(self, name):
        value = self.session_get(name)
        self.handler.clear_cookie(name)
        return value

    def session_setdefault(self, name, value):
        pass

    def build_absolute_uri(self, path=None):
        scheme = self.request.url.scheme
        host = self.request_host()
        return _build_absolute_uri(f"{scheme}://{host}", path)

    def partial_to_session(self, next, backend, request=None, *args, **kwargs):
        return json.dumps(
            super().partial_to_session(next, backend, request=request, *args, **kwargs)
        )

    def partial_from_session(self, session):
        if session:
            return super().partial_from_session(json.loads(session))


# --------------------------------------------------------------------------- #
# Route handlers (ported from app/handlers/auth.py)
# --------------------------------------------------------------------------- #
def _get_helper(handler, name):
    return handler.settings.get(setting_name(name), DEFAULTS.get(name, None))


def load_strategy(handler):
    return get_strategy(
        _get_helper(handler, "STRATEGY"), _get_helper(handler, "STORAGE"), handler
    )


def load_backend(handler, strategy, name, redirect_uri):
    backends = _get_helper(handler, "AUTHENTICATION_BACKENDS")
    Backend = get_backend(backends, name)
    return Backend(strategy, redirect_uri)


def psa(redirect_uri=None):
    def decorator(func):
        @wraps(func)
        def wrapper(self, backend, *args, **kwargs):
            uri = redirect_uri
            if uri and not uri.startswith("/"):
                uri = self.reverse_url(uri, backend)
            self.strategy = load_strategy(self)
            self.backend = load_backend(self, self.strategy, backend, uri)
            return func(self, backend, *args, **kwargs)

        return wrapper

    return decorator


class AuthHandler(Handler):
    def get(self, backend):
        return self._auth(backend)

    def post(self, backend):
        return self._auth(backend)

    @psa("complete")
    def _auth(self, backend):
        do_auth(self.backend)


class CompleteHandler(Handler):
    def get(self, backend):
        return self._complete(backend)

    def post(self, backend):
        return self._complete(backend)

    @psa("complete")
    def _complete(self, backend):
        do_complete(
            self.backend,
            login=lambda backend, user, social_user: self.login_user(user),
            user=self.current_user,
        )


class DisconnectHandler(Handler):
    @psa()
    def post(self, backend, association_id=None):
        do_disconnect(self.backend, self.current_user, association_id)


# Routes equivalent to baselayer's SOCIAL_AUTH_ROUTES (app/app_server.py).
SOCIAL_AUTH_ROUTES = [
    (r"/login/{backend}/", AuthHandler),
    (r"/complete/{backend}/", CompleteHandler),
    (r"/disconnect/{backend}/", DisconnectHandler),
]

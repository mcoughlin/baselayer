"""baselayer's own routes, assembled into a runnable Starlette/uvicorn app.

Part of the Tornado -> ASGI migration (issue #381). Ports the small baselayer
handlers (``ProfileHandler``/``LogoutHandler``/``SocketAuthTokenHandler`` from
``app/handlers/profile.py`` + ``socket_auth.py``) onto the compat ``Handler``
and wires them, together with the PSA login routes, into a Starlette app. This
is the "run baselayer under uvicorn" milestone.

``MainPageHandler`` (template rendering) is intentionally left out for now --
it needs a ``render()`` shim (Tornado template loader -> Jinja/Starlette
templates), tracked as the remaining baselayer route.

Run it (needs the app config + a migrated DB)::

    python -m baselayer.app.handlers.asgi_baselayer --config test_config.yaml
"""

from __future__ import annotations

import datetime

import jwt

from . import asgi_psa
from .asgi_compat import Handler, asgi_endpoint, authenticated

try:
    from starlette.applications import Starlette
    from starlette.routing import Route
except ImportError:  # pragma: no cover
    Starlette = Route = None  # type: ignore


class ProfileHandler(Handler):
    @authenticated
    def get(self):
        return self.success({"username": self.current_user.username})


class LogoutHandler(Handler):
    @authenticated
    def get(self):
        self.clear_cookie("user_id")
        self.redirect("/")


class SocketAuthTokenHandler(Handler):
    @authenticated
    def get(self):
        user = self.current_user
        secret = self.cfg["app.secret_key"]
        token = jwt.encode(
            {
                "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=15),
                "user_id": str(user.id),
            },
            secret,
        )
        self.success({"token": token})


# Mirrors the non-static routes in baselayer's app/app_server.py.
BASELAYER_ROUTES = [
    ("/baselayer/profile", ProfileHandler),
    ("/baselayer/logout", LogoutHandler),
    ("/baselayer/socket_auth_token", SocketAuthTokenHandler),
] + asgi_psa.SOCIAL_AUTH_ROUTES


def make_baselayer_asgi_app(settings, cfg):
    """Build the Starlette app for baselayer's own routes.

    ``settings``/``cfg`` are attached to the app object; the compat endpoint
    reads them off ``request.scope['app']`` so handlers see ``self.settings`` /
    ``self.cfg`` exactly as under Tornado.
    """
    # baselayer's settings point SOCIAL_AUTH_STRATEGY at the Tornado strategy;
    # force the ASGI one (storage is framework-agnostic and reused as-is).
    settings = dict(settings)
    settings["SOCIAL_AUTH_STRATEGY"] = (
        "baselayer.app.handlers.asgi_psa.StarletteStrategy"
    )
    settings.setdefault("SOCIAL_AUTH_STORAGE", "baselayer.app.psa.TornadoStorage")

    routes = [
        Route(
            path,
            asgi_endpoint(handler_cls),
            methods=["GET", "POST", "PUT", "DELETE"],
        )
        for path, handler_cls in BASELAYER_ROUTES
    ]
    app = Starlette(routes=routes)
    app.settings = settings
    app.cfg = cfg
    return app


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    from baselayer.app.env import load_env
    from baselayer.app.models import init_db

    env, cfg = load_env()
    init_db(**cfg["database"])

    from baselayer.app.app_server import settings as baselayer_settings

    baselayer_settings["cookie_secret"] = cfg["app.secret_key"]
    app = make_baselayer_asgi_app(baselayer_settings, cfg)
    uvicorn.run(app, host="127.0.0.1", port=8998)

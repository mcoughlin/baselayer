"""ASGI (Starlette/uvicorn) variant of ``services/app/app.py`` (issue #381).

Mirrors the Tornado entry point -- wait for the DB migration, load config, init
the DB, build the app -- but serves it under **uvicorn** instead of Tornado's
``IOLoop``. Kept parallel to ``app.py`` so the two can coexist during the
migration; switch the service over when ready.

The app is built via an ASGI factory (``app.asgi_factory``), defaulting to
baselayer's own routes. The full SkyPortal app -- its 211 handlers run through
the compat shim (``baselayer.app.handlers.asgi_compat``) -- is the next phase;
point ``app.asgi_factory`` at a SkyPortal ``make_asgi_app(settings, cfg)`` then.
"""

import importlib
import time

import requests
import uvicorn

from baselayer.app.env import load_env
from baselayer.log import make_log

env, cfg = load_env()
log = make_log(f"asgi_app_{env.process or 0}")

# Imported after load_env so its own load_env() doesn't fight arg parsing.
from baselayer.app.models import init_db  # noqa: E402


def migrated_db(migration_manager_port):
    try:
        r = requests.get(f"http://localhost:{migration_manager_port}")
        return r.json()["migrated"]
    except requests.exceptions.RequestException:
        log(f"Could not connect to migration manager on port [{migration_manager_port}]")
        return None


log("Verifying database migration status")
port = cfg["ports.migration_manager"]
timeout = 1
while not migrated_db(port):
    log(f"Database not migrated, or could not verify; trying again in {timeout}s")
    time.sleep(timeout)
    timeout = min(timeout * 2, 30)

init_db(**cfg["database"])

from baselayer.app.app_server import settings as baselayer_settings  # noqa: E402

baselayer_settings["cookie_secret"] = cfg["app.secret_key"]

factory_path = cfg.get(
    "app.asgi_factory",
    "baselayer.app.handlers.asgi_baselayer.make_baselayer_asgi_app",
)
module_name, factory_name = factory_path.rsplit(".", 1)
make_asgi_app = getattr(importlib.import_module(module_name), factory_name)
app = make_asgi_app(baselayer_settings, cfg)

port = cfg["ports.app_internal"] + (env.process or 0)
address = "127.0.0.1"
log(f"Listening (ASGI/uvicorn) on {address}:{port}")
uvicorn.run(app, host=address, port=port, proxy_headers=True, forwarded_allow_ips="*")

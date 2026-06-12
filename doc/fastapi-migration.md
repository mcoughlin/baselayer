# Migrating baselayer off Tornado (issue #381)

Status: **design + proof-of-concept**. This document captures the architecture
analysis and the migration strategy. The runnable PoC lives in
`baselayer/app/handlers/asgi_compat.py`.

## Goal

Replace Tornado as baselayer's HTTP layer with a modern ASGI stack
(Starlette + uvicorn), so we get:

- a faster, actively-maintained async server, and
- a path to free OpenAPI docs + request validation (via FastAPI, layered on
  later) instead of the hand-rolled machinery we maintain today.

The constraint that shapes everything below: **SkyPortal has 211 handler
classes across 105 files**, every one of them a Tornado `RequestHandler`
subclass written as `class XHandler(BaseHandler): async def get(self, id): …
self.success(data=…)`. A rewrite to FastAPI function-style routes is not
realistic as a first step. We therefore migrate the *server*, not the
*handlers*.

## What baselayer's web layer actually is

Mapped from the current `main` of this submodule:

| Piece | File | Role |
|---|---|---|
| HTTP server entry | `services/app/app.py` | waits for DB migration, builds the app via `cfg["app.factory"]` (SkyPortal's `make_app`), `app.listen(port)`, `IOLoop.start()` |
| App assembly | SkyPortal `skyportal/app_server.py::make_app` | `CustomApplication(tornado.web.Application)` = baselayer routes + skyportal routes |
| Base class | `app/handlers/base.py::BaseHandler` | the shared API every handler uses |
| Auth | `app/handlers/{auth,base}.py`, `app/psa.py` | python-social-auth (PSA), Tornado strategy/storage; login sets **signed cookies** |
| Routes | `app/app_server.py` | PSA login/complete/disconnect, `/baselayer/{profile,logout,socket_auth_token}`, mainpage, static |

### The `BaseHandler` API surface (what the shim must reproduce)

This is the entire contract the 211 handlers rely on:

- request: `self.request.body`, `.uri`, `.path`, `self.get_json()`,
  `self.get_query_argument(name, default, type=…)`, `self.get_argument(...)`,
  `self.path_args`
- response: `self.success(data=, action=, status=, extra=)`,
  `self.error(message, status=, data=)`, `self.write(...)`,
  `self.set_status(...)`, `self.set_header(...)`
- identity: `self.current_user`, `self.get_secure_cookie/set_secure_cookie`
- db: `self.Session()` (a `VerifiedSession` context manager bound to the user),
  `self.verify_and_commit()`
- realtime: `self.push(action, payload)`, `self.push_all(...)`,
  `self.push_notification(...)`, `self.flow`
- lifecycle: `prepare()`, `on_finish()` (`DBSession.remove()`),
  `write_error()`, `log_exception()`

### Two things that make this *much* easier than it looks

1. **WebSockets are already out-of-process.** `app/flow.py` is a thin ZeroMQ
   `PUSH` client; `push()`/`push_all()` send a message to the `websocket_server`
   service, which owns the browser sockets. The HTTP framework never touches a
   WebSocket. So migrating the HTTP server has *zero* WebSocket work.
2. **The DB session is request-scoped via `scoped_session`**, torn down in
   `on_finish` (`DBSession.remove()`). That maps cleanly onto ASGI middleware /
   per-request teardown — it does not depend on Tornado.

## Strategy: migrate the server, keep the handlers

Introduce a Starlette ASGI application plus a **compatibility `BaseHandler`**
that is *not* a Tornado `RequestHandler` but exposes the identical method
surface, backed by a Starlette `Request` and a buffered response. Each
`(route, HandlerClass)` tuple becomes a Starlette route whose endpoint:

1. instantiates the handler with the request,
2. runs `prepare()`,
3. dispatches to `get/post/put/delete` (sync or async) with path params,
4. turns the handler's buffered `write()`/`set_status`/`set_header` into a
   Starlette `Response`,
5. runs `on_finish()` (session teardown) in a `finally`.

Because the surface is preserved, **the 211 SkyPortal handlers run unchanged**
(modulo a handful of Tornado-specific escapes noted below). Tornado is removed
from the *server*, not from each endpoint.

uvicorn replaces `IOLoop`; `services/app/app.py` becomes
`uvicorn.run(asgi_app, ...)`. FastAPI can be layered on top *later* (it is
Starlette underneath) to get OpenAPI/validation for new or incrementally
migrated endpoints, without touching the legacy ones.

### Why not "just rewrite to FastAPI"

211 handler classes, many with deep permission logic, custom query params, file
uploads, and streamed downloads. A big-bang rewrite is a multi-quarter effort
with high regression risk and no intermediate shippable state. The shim lets us
flip the server in one reviewable change and migrate endpoints to native
FastAPI opportunistically afterwards.

## The hard parts (and how to handle them)

1. **Signed cookies — DONE.** `app/handlers/secure_cookie.py` is a standalone,
   byte-compatible port of Tornado's v2 `create_signed_value` /
   `decode_signed_value` (HMAC-SHA256, **no tornado dependency**).
   `tests/test_secure_cookie.py` proves byte-equality against a golden Tornado
   vector *and* against a live tornado (cross-decode both directions), plus
   wrong-secret/wrong-name/tampered/expired rejection — so existing logged-in
   sessions survive the cutover. Wired into the shim's `get/set_secure_cookie`.
2. **PSA auth flow.** `social-auth-core` ships a Tornado strategy/storage
   (`app/psa.py`). `social-core` also ships `social_core.strategy` bases; we need
   a small Starlette strategy (request/response/redirect/session adapters) to
   replace `TornadoStrategy`. This is the single most involved piece; isolate it
   behind the `/login`, `/complete`, `/disconnect` routes.
3. **Route translation.** Tornado routes are regexes with named groups
   (`(?P<backend>[^/]+)`); Starlette uses `/{backend}` path params (or `Mount` +
   regex). Provide a `tornado_route_to_starlette()` helper; named groups → path
   params, positional groups → ordered `path_args`. SkyPortal already migrated
   most path params to `: int`-annotated method args, which maps directly.
4. **Static & file responses.** `tornado.web.StaticFileHandler` →
   `starlette.staticfiles.StaticFiles`. Streamed/large downloads (FITS, CSV)
   that use `self.write()` in a loop → Starlette `StreamingResponse`; audit
   handlers that call `self.flush()`.
5. **Multipart / large uploads.** Tornado buffers the whole body; Starlette
   gives `await request.form()` / `request.stream()`. `self.request.body` must be
   populated (await the body) before sync handlers run.
6. **`get_argument` semantics.** Tornado's `get_argument` raises
   `MissingArgumentError` (→ 400) when required and absent; replicate that in the
   shim so validation behavior is unchanged.
7. **`write_error` / exception mapping.** `AccessError → 401`, the
   expected-exception log filtering in `log_exception`, and rendering
   `loginerror.html` for the PSA handler — reproduce in an ASGI exception
   handler.

## Phased plan

1. **Shim + smoke test (this branch).** `asgi_compat.py` with the
   request/response adapter, dispatch, `success`/`error`/`get_json`/query-arg/
   secure-cookie, and a Starlette app that serves one trivial handler. Prove a
   request round-trips and `current_user` resolves from an existing cookie.
2. **Port baselayer's own routes** (profile, logout, socket_auth_token,
   mainpage, static) + the PSA strategy. baselayer is self-contained and testable
   without SkyPortal.
3. **Swap the server in SkyPortal.** Point `cfg["app.factory"]` at an
   ASGI `make_app`; run under uvicorn in `services/app/app.py`. Run SkyPortal's
   API test suite against it — it exercises the handlers exhaustively and is the
   real acceptance test.
4. **Burn down Tornado escapes.** Grep SkyPortal for direct `tornado.*` use
   inside handlers (flush/redirect/render/set_cookie/RequestHandler internals)
   and replace with shim equivalents.
5. **Cut over + delete Tornado.** Remove `tornado` from `pyproject.toml`.
6. **(Optional, later) FastAPI-native endpoints.** New/rewritten endpoints use
   FastAPI routes for OpenAPI + pydantic validation, coexisting with shimmed ones.

## Risks / open questions

- **Secure-cookie compatibility** is the deploy-safety crux; verify a cookie
  minted by current Tornado decodes in the shim (and vice-versa) before cutover.
- **Sync vs async handlers.** Tornado runs sync `get/post` on the IOLoop;
  under ASGI, blocking sync handlers must go through `run_in_threadpool` or be
  made async. Most SkyPortal handlers are already `async def`.
- **`self.application.cfg`** and other `self.application.*` lookups — provide an
  app-like object on the shim (`self.app`) carrying `cfg`.
- Per-request `DBSession` teardown must run on *every* exit path (success,
  error, exception, client disconnect) — enforce in a `finally`/middleware.

## Status in this branch

- `app/handlers/asgi_compat.py` — compat `BaseHandler` + Starlette adapter
  (request/response/dispatch, sync+async; **Tornado-compatible signed cookies**
  and a **real `current_user`** wired in). Smoke test passes (`/ping`, `/echo`,
  405).
- `app/handlers/secure_cookie.py` — standalone byte-compatible port of Tornado's
  v2 signed cookies (**done + tested**).
- `tests/test_secure_cookie.py` — golden-vector + live-tornado cross-checks.
- `pyproject.toml` — adds `starlette`, `uvicorn` (kept alongside `tornado`).

### Auth read path — DONE + verified live

`current_user` ports `PSABaseHandler.get_current_user` faithfully (signed
`user_id`/`user_oauth_uid` cookies → `User`, with the `SocialAuth.uid`
cross-check; machine users with no SocialAuth row accepted). **Verified against
the running server**: a cookie minted with the server's real `app.secret_key`
resolved the real `skyportal_test` user (`id=1 provisioned-admin`) through the
shim. SkyPortal additionally accepts `Authorization`-header tokens; that
override lives in *its* `BaseHandler` and layers on top of this.

Next concrete step (auth **write** path / login): replace
`TornadoStrategy`/`TornadoStorage` (`app/psa.py`) with a small Starlette
python-social-auth strategy (request/response/redirect/session adapters) so the
OAuth dance at `/login/*` and `/complete/*` runs under uvicorn. With that +
`current_user`, baselayer's own routes (`/baselayer/profile|logout`,
`socket_auth_token`, mainpage) are fully runnable on the ASGI stack.

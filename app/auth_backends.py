"""Which python-social-auth backends the app offers for sign-in.

Configured through `server.auth.backends`; when that is empty we fall back to
Google via the legacy `server.auth.google_oauth2_*` keys, so existing
deployments keep working untouched.
"""

from .env import load_env

env, cfg = load_env()

GOOGLE_BACKEND = {
    "name": "google-oauth2",
    "class": "social_core.backends.google.GoogleOAuth2",
    "label": "Sign in with Google",
}


def setting_prefix(name):
    """The SOCIAL_AUTH_<prefix>_* namespace a backend reads its settings from.

    Mirrors social_core.utils.to_setting_name, which is what python-social-auth
    uses to look up per-backend settings.
    """
    return name.upper().replace("-", "_")


def default_auth_backend():
    """Backend name to use where exactly one must be named: invite links, and
    users created outside the OAuth flow (initial setup, `add_user`)."""
    return configured_backends()[0]["name"]


def configured_backends():
    """Normalized list of the enabled backends, in the order they are offered.

    Each entry has `name` (the backend's own name, i.e. what appears in
    /login/<name>), `class` (dotted path), `key`, `secret`, `label` and a
    `settings` dict of extra per-backend settings (e.g. OIDC_ENDPOINT), which
    are exported as SOCIAL_AUTH_<prefix>_<KEY>.
    """
    backends = cfg.get("server.auth.backends") or []
    if not backends:
        backends = [
            {
                **GOOGLE_BACKEND,
                "key": cfg["server.auth.google_oauth2_key"],
                "secret": cfg["server.auth.google_oauth2_secret"],
            }
        ]

    normalized = []
    for backend in backends:
        name, class_path = backend.get("name"), backend.get("class")
        if not name or not class_path:
            raise ValueError(
                f"server.auth.backends entry {backend!r} needs both `name` "
                "(the backend's own name) and `class` (its dotted path)"
            )
        normalized.append(
            {
                "name": name,
                "class": class_path,
                "key": backend.get("key"),
                "secret": backend.get("secret"),
                "label": backend.get("label")
                or (GOOGLE_BACKEND["label"] if name == "google-oauth2" else None)
                or f"Sign in with {name}",
                "settings": backend.get("settings") or {},
            }
        )
    return normalized

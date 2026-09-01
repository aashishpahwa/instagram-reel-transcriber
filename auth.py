"""JWT/JWKS verification for Neon Auth (Managed Better Auth).

Neon Auth runs as a separate REST service on its own subdomain - there's no
Flask SDK and no shared-cookie story (different origins). The frontend signs
in against that service directly and exchanges its session for a short-lived
(15 min) JWT, sent here as `Authorization: Bearer <jwt>` on every API call.
This module verifies that JWT via the service's JWKS endpoint.
"""
import os
import threading
from functools import wraps
from urllib.parse import urlparse

import jwt
from flask import g, jsonify, request
from jwt import PyJWKClient

NEON_AUTH_BASE_URL = os.environ["NEON_AUTH_BASE_URL"].rstrip("/")
NEON_AUTH_JWKS_URL = f"{NEON_AUTH_BASE_URL}/.well-known/jwks.json"
_parsed = urlparse(NEON_AUTH_BASE_URL)
NEON_AUTH_ISSUER = os.environ.get("NEON_AUTH_ISSUER") or f"{_parsed.scheme}://{_parsed.netloc}"

_jwk_client = PyJWKClient(NEON_AUTH_JWKS_URL, cache_keys=True, lifespan=3600)


def verify_token(token):
    """Returns the decoded JWT payload, or None if invalid/expired/malformed."""
    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token)
        return jwt.decode(token, signing_key.key, algorithms=["EdDSA"],
                           issuer=NEON_AUTH_ISSUER, audience=NEON_AUTH_ISSUER)
    except jwt.PyJWTError:
        return None


_ensured_users = set()
_ensured_lock = threading.Lock()


def ensure_app_user(user_id, email):
    """Lazily mirrors a Neon Auth user into app_users on first authenticated
    request - avoids needing a publicly reachable webhook receiver, which a
    localhost dev app can't offer anyway. Done once per process per user: this
    runs on every request, and an INSERT is three round trips to Neon."""
    with _ensured_lock:
        if user_id in _ensured_users:
            return
    from app import db_cursor  # local import: app.py imports this module too
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO app_users (id, email) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (user_id, email),
        )
    with _ensured_lock:
        _ensured_users.add(user_id)


def require_login(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "unauthorized"}), 401
        payload = verify_token(auth_header[7:])
        if not payload:
            return jsonify({"error": "unauthorized"}), 401
        g.user_id = payload["sub"]
        g.user_email = payload.get("email")
        ensure_app_user(g.user_id, g.user_email)
        return fn(*args, **kwargs)
    return wrapper

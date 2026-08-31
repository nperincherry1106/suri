"""Apple Sign-In identity-token validation + session JWT issuance/verification.

Auth flow (native iOS, the only flow we support in v0):

  1. iOS app calls ASAuthorizationAppleIDProvider, gets back an `identityToken`
     (a JWT signed by Apple with one of their RS256 keys).
  2. App POSTs the token to /api/v1/auth/apple.
  3. We fetch Apple's JWKS (cached for an hour), verify the JWT's signature,
     check `iss == https://appleid.apple.com`, `aud == APPLE_BUNDLE_ID`, and
     that the token isn't expired.
  4. We extract `sub` (Apple's stable per-user identifier).
  5. Single-user gate: `sub` must equal `SURI_OWNER_APPLE_SUB`. If the env
     var is unset, we log the `sub` and refuse — Namrita then sets the secret
     to lock Suri to her Apple ID. (Bootstrap chicken-and-egg solved by a
     one-time log line.)
  6. We mint our own session JWT (HS256 with `SURI_SESSION_SECRET`) carrying
     `sub`, a unique `jti`, `iat`, `exp` (90 days), and `iss=suri`.
  7. We persist the `jti` to `app_sessions` so we can revoke individual
     sessions without rotating the global signing secret.

Subsequent /api/v1/* requests carry `Authorization: Bearer <session_jwt>`.
The `current_apple_sub` FastAPI dependency does the verify+lookup+touch dance.
"""
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone

import jwt
import requests
from fastapi import Depends, HTTPException, Request

from app import db


APPLE_ISS = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"

# How long a session JWT is valid. iOS keychain holds it; user re-signs in
# from the app's "you" tab if it expires. 90d matches Outlook refresh-token
# semantics so all our trust horizons line up.
SESSION_TTL = timedelta(days=90)

# Allowed clock skew when validating Apple's token. Apple's docs recommend
# ~5 minutes. Strictly we should sync with NTP; in practice fly machines do.
APPLE_CLOCK_SKEW_SECONDS = 300


# ---------------------------------------------------------------------------
# Apple JWKS fetch + cache
# ---------------------------------------------------------------------------

# Apple rotates JWKS keys infrequently (months), but we still cache short
# enough that a forced rotation propagates within an hour. The cache is
# in-process; multi-machine setups would need a shared cache, but we're
# single-machine for v0.
_JWKS_CACHE: dict | None = None
_JWKS_FETCHED_AT: float = 0.0
_JWKS_TTL_SECONDS = 3600


def _fetch_apple_jwks(force: bool = False) -> dict:
    """Return Apple's current JWKS dict, fetching from Apple if our cache
    is stale or `force=True`. Raises on network failure — callers translate
    into HTTP 503 so the iOS app can retry."""
    global _JWKS_CACHE, _JWKS_FETCHED_AT
    now = time.time()
    if not force and _JWKS_CACHE is not None and (now - _JWKS_FETCHED_AT) < _JWKS_TTL_SECONDS:
        return _JWKS_CACHE
    r = requests.get(APPLE_JWKS_URL, timeout=10)
    r.raise_for_status()
    _JWKS_CACHE = r.json()
    _JWKS_FETCHED_AT = now
    return _JWKS_CACHE


def _public_key_for_kid(kid: str, allow_refetch: bool = True):
    """Find the JWK matching `kid` and convert it to a cryptography
    RSAPublicKey object PyJWT can use. If not found and we haven't already
    re-fetched, force a JWKS refresh — covers the case where Apple rotated
    keys mid-cache-window."""
    jwks = _fetch_apple_jwks()
    for jwk in jwks.get("keys", []):
        if jwk.get("kid") == kid:
            return jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
    if allow_refetch:
        _fetch_apple_jwks(force=True)
        return _public_key_for_kid(kid, allow_refetch=False)
    raise HTTPException(
        status_code=401,
        detail=f"unknown apple signing key (kid={kid})",
    )


# ---------------------------------------------------------------------------
# Apple identity token verification
# ---------------------------------------------------------------------------


def _required_env(key: str) -> str:
    v = (os.environ.get(key) or "").strip()
    if not v:
        raise HTTPException(
            status_code=503,
            detail=f"server misconfigured: {key} not set",
        )
    return v


def verify_apple_identity_token(id_token: str) -> dict:
    """Verify Apple's identity token and return its decoded payload. Raises
    HTTPException on any validation failure with a useful detail. Does NOT
    enforce the single-user gate — that's done in the route handler so we
    can log the unknown `sub` separately."""
    try:
        unverified_header = jwt.get_unverified_header(id_token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"malformed identity_token: {e}")
    kid = unverified_header.get("kid")
    if not kid:
        raise HTTPException(status_code=401, detail="identity_token missing kid header")

    public_key = _public_key_for_kid(kid)
    audience = _required_env("APPLE_BUNDLE_ID")

    try:
        payload = jwt.decode(
            id_token,
            public_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=APPLE_ISS,
            leeway=APPLE_CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="identity_token expired")
    except jwt.InvalidAudienceError:
        raise HTTPException(
            status_code=401,
            detail=f"identity_token aud doesn't match APPLE_BUNDLE_ID ({audience})",
        )
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=401, detail="identity_token iss isn't apple")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"identity_token invalid: {e}")

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="identity_token missing sub")
    return payload


# ---------------------------------------------------------------------------
# Single-user gate
# ---------------------------------------------------------------------------


def is_owner(apple_sub: str) -> tuple[bool, str | None]:
    """Returns (allowed, reason_if_denied). On the very first sign-in
    (SURI_OWNER_APPLE_SUB unset), we deny but log the apple_sub at INFO
    level so Namrita can copy it from fly logs and pin it via:
        fly secrets set -a suri SURI_OWNER_APPLE_SUB=<the_sub>
    """
    pinned = (os.environ.get("SURI_OWNER_APPLE_SUB") or "").strip()
    if not pinned:
        print(
            f"[auth] FIRST SIGN-IN: apple_sub={apple_sub!r}. "
            f"Pin this with: fly secrets set -a suri SURI_OWNER_APPLE_SUB={apple_sub}",
            file=sys.stderr,
            flush=True,
        )
        return False, "owner not yet pinned; check fly logs for the apple_sub then set SURI_OWNER_APPLE_SUB"
    if apple_sub != pinned:
        print(
            f"[auth] DENIED non-owner sign-in attempt: apple_sub={apple_sub!r}",
            file=sys.stderr,
            flush=True,
        )
        return False, "this suri is single-tenant"
    return True, None


# ---------------------------------------------------------------------------
# Session JWT (HS256, our own signing key)
# ---------------------------------------------------------------------------


def issue_session_jwt(apple_sub: str, user_agent: str | None = None) -> dict:
    """Mint a fresh session JWT and persist its jti. Returns dict with
    `session_jwt` (string), `expires_at` (ISO 8601 UTC) — the exact shape
    the iOS app stores in keychain."""
    secret = _required_env("SURI_SESSION_SECRET")
    now = datetime.now(timezone.utc)
    exp = now + SESSION_TTL
    jti = secrets.token_urlsafe(16)
    payload = {
        "iss": "suri",
        "sub": apple_sub,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    db.create_app_session(
        jti=jti,
        apple_sub=apple_sub,
        expires_at=exp.isoformat(),
        user_agent=user_agent,
    )
    return {
        "session_jwt": token,
        "expires_at": exp.isoformat(),
    }


def verify_session_jwt(token: str) -> str:
    """Verify a session JWT and return the apple_sub. Raises HTTPException
    on signature failure, expiry, or revocation."""
    secret = _required_env("SURI_SESSION_SECRET")
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            issuer="suri",
            options={"require": ["exp", "iat", "sub", "jti", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="session expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"session invalid: {e}")

    jti = payload.get("jti")
    sub = payload.get("sub")
    if not jti or not sub:
        raise HTTPException(status_code=401, detail="session token missing jti/sub")

    row = db.get_app_session(jti)
    if row is None:
        # Token signature checks out but we have no record. Either the DB
        # was wiped (acceptable in v0 — re-sign-in) or the token was minted
        # against a different DB. Either way, deny.
        raise HTTPException(status_code=401, detail="session not found")
    if row.get("revoked"):
        raise HTTPException(status_code=401, detail="session revoked")
    db.touch_app_session(jti)
    return sub


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def current_apple_sub(request: Request) -> str:
    """FastAPI dependency. Pulls the bearer token off the Authorization
    header, verifies it, returns the apple_sub. Use as:

        @router.get("/api/v1/me")
        def me(sub: str = Depends(current_apple_sub)):
            return {"sub": sub}
    """
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = auth.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="empty bearer token")
    return verify_session_jwt(token)


# Re-export so route modules can `from app.auth import Depends` if helpful.
__all__ = [
    "verify_apple_identity_token",
    "is_owner",
    "issue_session_jwt",
    "verify_session_jwt",
    "current_apple_sub",
    "Depends",
]

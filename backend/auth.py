"""OIDC access-token verification and request identity for the HTTP service."""
from __future__ import annotations

import os
from dataclasses import dataclass

import jwt
from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class AuthConfig:
    mode: str
    issuer: str = ""
    audience: str = ""
    jwks_url: str = ""

    @classmethod
    def from_env(cls) -> AuthConfig:
        environment = os.getenv("CAMHAL_ENV", "development").lower()
        if environment not in {"development", "production"}:
            raise ValueError("CAMHAL_ENV must be 'development' or 'production'")
        mode = os.getenv("CAMHAL_AUTH_MODE", "local").lower()
        if mode not in {"local", "oidc"}:
            raise ValueError("CAMHAL_AUTH_MODE must be 'local' or 'oidc'")
        if environment == "production" and mode != "oidc":
            raise ValueError("Production requires CAMHAL_AUTH_MODE=oidc")
        if mode == "local":
            return cls(mode)
        issuer = os.getenv("CAMHAL_OIDC_ISSUER", "")
        audience = os.getenv("CAMHAL_OIDC_AUDIENCE", "")
        jwks_url = os.getenv("CAMHAL_OIDC_JWKS_URL", "")
        if not issuer.startswith("https://") or not audience or not jwks_url.startswith("https://"):
            raise ValueError("OIDC requires HTTPS CAMHAL_OIDC_ISSUER and CAMHAL_OIDC_JWKS_URL, "
                             "plus CAMHAL_OIDC_AUDIENCE")
        return cls(mode, issuer, audience, jwks_url)


class TokenVerifier:
    def __init__(self, config: AuthConfig):
        self.config = config
        self.jwks = jwt.PyJWKClient(config.jwks_url, cache_jwk_set=True, lifespan=300, timeout=3)

    def verify(self, token: str) -> str:
        if not token or len(token) > 8192:
            raise jwt.InvalidTokenError("Invalid token length")
        key = self.jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"],
                            issuer=self.config.issuer, audience=self.config.audience,
                            options={"require": ["exp", "iss", "aud", "sub"]})
        subject = claims["sub"]
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise jwt.InvalidTokenError("Invalid subject")
        return subject


class IdentityMiddleware:
    def __init__(self, app: ASGIApp, config: AuthConfig, verifier: TokenVerifier | None = None):
        self.app = app
        self.config = config
        self.verifier = verifier if verifier is not None else (TokenVerifier(config) if config.mode == "oidc" else None)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if scope["path"] in {"/api/health", "/api/ready"}:
            await self.app(scope, receive, send)
            return
        if self.config.mode == "local":
            scope.setdefault("state", {})["owner"] = "local"
        else:
            authorization = request.headers.get("authorization", "")
            if not authorization.startswith("Bearer ") or self.verifier is None:
                await self._reject(scope, receive, send)
                return
            try:
                scope.setdefault("state", {})["owner"] = await run_in_threadpool(
                    self.verifier.verify, authorization[7:])
            except (jwt.PyJWTError, jwt.PyJWKClientError, ValueError, OSError):
                await self._reject(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"detail": "인증이 필요합니다."}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        await response(scope, receive, send)

"""Local-only OIDC issuer used by scripts/smoke_oidc.sh."""
from __future__ import annotations

import json
import ssl
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


ISSUER = "https://oidc.test:4443"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
JWK.update({"kid": "smoke-key", "alg": "RS256", "use": "sig"})
CODES: dict[str, tuple[str, str]] = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass

    def json_response(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/.well-known/openid-configuration":
            self.json_response({
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/auth",
                "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/keys",
                "userinfo_endpoint": f"{ISSUER}/userinfo",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "email", "profile", "camhal-api"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                "claims_supported": ["sub", "email", "email_verified", "preferred_username"],
            })
            return
        if parsed.path == "/keys":
            self.json_response({"keys": [JWK]})
            return
        if parsed.path == "/auth":
            query = parse_qs(parsed.query)
            code = uuid.uuid4().hex
            user = "user-b" if "idp_user=b" in self.headers.get("Cookie", "") else "user-a"
            CODES[code] = (user, query.get("nonce", [""])[0])
            redirect = query["redirect_uri"][0]
            separator = "&" if "?" in redirect else "?"
            location = redirect + separator + urlencode({"code": code, "state": query["state"][0]})
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
            return
        if parsed.path == "/userinfo":
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            try:
                claims = jwt.decode(token, PRIVATE_KEY.public_key(), algorithms=["RS256"],
                                    issuer=ISSUER, audience="camhal-api")
            except jwt.PyJWTError:
                self.json_response({"error": "invalid_token"}, 401)
                return
            user = claims["sub"]
            self.json_response({"sub": user, "email": f"{user}@example.com",
                                "email_verified": True, "preferred_username": user})
            return
        self.json_response({"error": "not_found"}, 404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/token":
            self.json_response({"error": "not_found"}, 404)
            return
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
        details = CODES.pop(form.get("code", [""])[0], None)
        if details is None:
            self.json_response({"error": "invalid_grant"}, 400)
            return
        user, nonce = details
        now = int(time.time())
        common = {"iss": ISSUER, "sub": user, "iat": now, "exp": now + 3600}
        id_claims = {**common, "aud": "camhal-web", "email": f"{user}@example.com",
                     "email_verified": True, "preferred_username": user}
        if nonce:
            id_claims["nonce"] = nonce
        access_claims = {**common, "aud": "camhal-api", "scope": "openid email profile camhal-api"}
        self.json_response({
            "access_token": jwt.encode(access_claims, PRIVATE_KEY, algorithm="RS256",
                                       headers={"kid": "smoke-key"}),
            "id_token": jwt.encode(id_claims, PRIVATE_KEY, algorithm="RS256",
                                   headers={"kid": "smoke-key"}),
            "token_type": "Bearer",
            "expires_in": 3600,
        })


if __name__ == "__main__":
    cert_dir = Path(sys.argv[1])
    server = ThreadingHTTPServer(("0.0.0.0", 4443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_dir / "idp.crt", cert_dir / "idp.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()

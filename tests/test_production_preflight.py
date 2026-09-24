"""Production configuration rejects placeholders and mismatched TLS material."""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from scripts.preflight_production import validate


def config() -> dict:
    return {
        "services": {
            "camhal": {"environment": {
                "CAMHAL_ENV": "production", "CAMHAL_AUTH_MODE": "oidc",
                "CAMHAL_OIDC_ISSUER": "https://id.camhal.test/realms/camhal",
                "CAMHAL_OIDC_JWKS_URL": "https://id.camhal.test/keys",
                "CAMHAL_OIDC_AUDIENCE": "camhal-api",
            }},
            "oauth2-proxy": {"environment": {
                "OAUTH2_PROXY_OIDC_ISSUER_URL": "https://id.camhal.test/realms/camhal",
                "OAUTH2_PROXY_REDIRECT_URL": "https://camhal.test/oauth2/callback",
                "OAUTH2_PROXY_CLIENT_ID": "camhal-web",
                "OAUTH2_PROXY_CLIENT_SECRET": "provider-issued-secret",
                "OAUTH2_PROXY_EMAIL_DOMAINS": "camhal.test",
                "OAUTH2_PROXY_COOKIE_SECRET": base64.urlsafe_b64encode(os.urandom(32)).decode(),
                "OAUTH2_PROXY_TRUSTED_PROXY_IPS": "172.30.250.10",
                "OAUTH2_PROXY_SCOPE": "openid email profile camhal-api",
                "OAUTH2_PROXY_PASS_ACCESS_TOKEN": "true",
                "OAUTH2_PROXY_PASS_AUTHORIZATION_HEADER": "false",
                "OAUTH2_PROXY_COOKIE_SECURE": "true",
            }},
            "nginx": {"networks": {"auth-net": {"ipv4_address": "172.30.250.10"}}},
        },
        "networks": {"auth-net": {"ipam": {"config": [{"subnet": "172.30.250.0/24"}]}}},
    }


def write_certificate(directory: Path, hostname: str = "camhal.test", matching_key: bool = True) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .sign(key, hashes.SHA256()))
    (directory / "fullchain.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    if not matching_key:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = directory / "privkey.pem"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))
    key_path.chmod(0o600)


class ProductionPreflightTest(unittest.TestCase):
    def test_matching_config_and_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_certificate(Path(tmp))
            self.assertEqual(validate(config(), Path(tmp)), [])

    def test_rejects_placeholders_and_tls_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_certificate(Path(tmp), hostname="other.test", matching_key=False)
            broken = config()
            app = broken["services"]["camhal"]["environment"]
            oauth = broken["services"]["oauth2-proxy"]["environment"]
            app["CAMHAL_OIDC_ISSUER"] = "https://id.example.com/realms/camhal"
            oauth["OAUTH2_PROXY_OIDC_ISSUER_URL"] = app["CAMHAL_OIDC_ISSUER"]
            oauth["OAUTH2_PROXY_REDIRECT_URL"] = "https://camhal.example.com/oauth2/callback"
            oauth["OAUTH2_PROXY_CLIENT_SECRET"] = "replace-with-provider-client-secret"
            oauth["OAUTH2_PROXY_COOKIE_SECRET"] = "replace-with-random-base64-32-byte-secret"
            errors = validate(broken, Path(tmp))
            self.assertTrue(any("예시 도메인" in error for error in errors))
            self.assertTrue(any("CAMHAL_COOKIE_SECRET" in error for error in errors))
            self.assertTrue(any("TLS 인증서와 개인 키" in error for error in errors))


if __name__ == "__main__":
    unittest.main()

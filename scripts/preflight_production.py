"""Check production Compose inputs before creating public containers."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import serialization


ROOT = Path(__file__).resolve().parent.parent


def _https_host(value: str, label: str, errors: list[str]) -> str | None:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        errors.append(f"{label}: URL 형식이 잘못되었습니다.")
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        errors.append(f"{label}: HTTPS URL이 필요합니다.")
        return None
    if parsed.hostname.endswith("example.com"):
        errors.append(f"{label}: 예시 도메인을 실제 값으로 바꾸세요.")
    return parsed.hostname.lower()


def _certificate_matches(cert: x509.Certificate, host: str) -> bool:
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip in names.get_values_for_type(x509.IPAddress)
    except ValueError:
        pass
    for name in names.get_values_for_type(x509.DNSName):
        name = name.lower()
        if name == host or (name.startswith("*.") and host.endswith(name[1:])
                            and host.count(".") == name.count(".")):
            return True
    return False


def validate(config: dict, cert_dir: Path) -> list[str]:
    errors: list[str] = []
    services = config.get("services", {})
    app = services.get("camhal", {}).get("environment", {})
    oauth = services.get("oauth2-proxy", {}).get("environment", {})
    if app.get("CAMHAL_ENV") != "production" or app.get("CAMHAL_AUTH_MODE") != "oidc":
        errors.append("앱은 production/OIDC 모드여야 합니다.")

    issuer = app.get("CAMHAL_OIDC_ISSUER", "")
    _https_host(issuer, "CAMHAL_OIDC_ISSUER", errors)
    _https_host(app.get("CAMHAL_OIDC_JWKS_URL", ""), "CAMHAL_OIDC_JWKS_URL", errors)
    if oauth.get("OAUTH2_PROXY_OIDC_ISSUER_URL") != issuer:
        errors.append("앱과 로그인 프록시의 OIDC 발급자가 다릅니다.")
    if not app.get("CAMHAL_OIDC_AUDIENCE"):
        errors.append("CAMHAL_OIDC_AUDIENCE가 비어 있습니다.")
    try:
        total_limit = int(app.get("CAMHAL_MAX_RUNS", ""))
        owner_limit = int(app.get("CAMHAL_MAX_RUNS_PER_OWNER", ""))
        if total_limit < 1 or not 1 <= owner_limit <= total_limit:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("실행 기록 한도는 양의 정수이고 사용자별 한도가 전체 한도 이하여야 합니다.")
    if "openid" not in oauth.get("OAUTH2_PROXY_SCOPE", "").split():
        errors.append("CAMHAL_OIDC_SCOPE에 openid가 필요합니다.")
    if (oauth.get("OAUTH2_PROXY_PASS_ACCESS_TOKEN") != "true" or
            oauth.get("OAUTH2_PROXY_PASS_AUTHORIZATION_HEADER") != "false" or
            oauth.get("OAUTH2_PROXY_COOKIE_SECURE") != "true"):
        errors.append("로그인 프록시의 액세스 토큰 전달·헤더 차단·보안 쿠키 설정을 확인하세요.")

    redirect = oauth.get("OAUTH2_PROXY_REDIRECT_URL", "")
    public_host = _https_host(redirect, "CAMHAL_PUBLIC_URL", errors)
    try:
        parsed_redirect = urlsplit(redirect)
        if parsed_redirect.path != "/oauth2/callback" or parsed_redirect.query or parsed_redirect.fragment:
            errors.append("로그인 리디렉션 경로는 /oauth2/callback이어야 합니다.")
    except ValueError:
        pass
    for key in ("OAUTH2_PROXY_CLIENT_ID", "OAUTH2_PROXY_CLIENT_SECRET"):
        value = oauth.get(key, "")
        if not value or value.startswith("replace-"):
            errors.append(f"{key}: 실제 OIDC 공급자 값을 입력하세요.")
    allowed_domain = oauth.get("OAUTH2_PROXY_EMAIL_DOMAINS", "")
    if not allowed_domain or allowed_domain == "example.com":
        errors.append("CAMHAL_ALLOWED_EMAIL_DOMAIN: 실제 허용 도메인을 입력하세요.")
    secret = oauth.get("OAUTH2_PROXY_COOKIE_SECRET", "")
    try:
        decoded = base64.b64decode(secret, altchars=b"-_", validate=True)
        if len(decoded) != 32:
            raise ValueError
    except (ValueError, binascii.Error):
        errors.append("CAMHAL_COOKIE_SECRET: Base64로 인코딩한 32바이트 난수가 필요합니다.")

    try:
        subnet = ipaddress.ip_network(config["networks"]["auth-net"]["ipam"]["config"][0]["subnet"])
        proxy_ip = ipaddress.ip_address(oauth["OAUTH2_PROXY_TRUSTED_PROXY_IPS"])
        assigned = ipaddress.ip_address(services["nginx"]["networks"]["auth-net"]["ipv4_address"])
        if proxy_ip not in subnet or assigned != proxy_ip:
            errors.append("CAMHAL_PROXY_IP가 인증 네트워크의 Nginx 주소와 일치하지 않습니다.")
    except (KeyError, IndexError, ValueError) as exc:
        errors.append(f"인증 네트워크 설정을 확인할 수 없습니다: {type(exc).__name__}")

    certificate_path = cert_dir / "fullchain.pem"
    key_path = cert_dir / "privkey.pem"
    if not certificate_path.is_file() or not key_path.is_file():
        errors.append("deploy/tls/fullchain.pem과 privkey.pem이 모두 필요합니다.")
        return errors
    if key_path.stat().st_mode & 0o077:
        errors.append("TLS 개인 키는 소유자만 읽을 수 있도록 chmod 600을 설정하세요.")
    try:
        cert = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        now = datetime.now(timezone.utc)
        if cert.not_valid_before_utc > now or cert.not_valid_after_utc <= now:
            errors.append("TLS 인증서가 현재 유효하지 않습니다.")
        if public_host and not _certificate_matches(cert, public_host):
            errors.append("TLS 인증서 SAN에 공개 도메인이 없습니다.")
        cert_public = cert.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        key_public = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if cert_public != key_public:
            errors.append("TLS 인증서와 개인 키가 일치하지 않습니다.")
    except (ValueError, TypeError, OSError) as exc:
        errors.append(f"TLS 인증서 또는 개인 키를 읽을 수 없습니다: {type(exc).__name__}")
    return errors


def main() -> None:
    env_file = ROOT / ".env.production"
    if not env_file.is_file():
        raise SystemExit(".env.production 파일이 없습니다.")
    if env_file.stat().st_mode & 0o077:
        raise SystemExit(".env.production에는 비밀값이 있습니다. chmod 600을 설정하세요.")
    try:
        probe = subprocess.run(["docker", "compose", "version"], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        executable = ["docker", "compose"] if probe.returncode == 0 else ["docker-compose"]
    except FileNotFoundError:
        executable = ["docker-compose"]
    command = [*executable, "--env-file", str(env_file), "-f", str(ROOT / "compose.production.yaml"),
               "config", "--format", "json"]
    try:
        result = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit("Docker Compose 명령을 찾지 못했습니다.") from None
    if result.returncode:
        raise SystemExit("Compose 설정을 읽지 못했습니다. .env.production과 compose.production.yaml을 확인하세요.")
    errors = validate(json.loads(result.stdout), ROOT / "deploy" / "tls")
    if errors:
        raise SystemExit("운영 설정 사전 점검 실패:\n- " + "\n- ".join(errors))
    print("운영 설정 사전 점검 통과: 환경 변수, 네트워크, TLS 인증서와 키")


if __name__ == "__main__":
    main()

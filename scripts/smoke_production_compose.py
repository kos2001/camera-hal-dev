"""Run the production Compose topology with a disposable local OIDC issuer."""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent


def curl(*args: str) -> str:
    return subprocess.check_output(["curl", "-sSk", "--noproxy", "*", *args], text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="운영 Compose 토폴로지 로그인 검증")
    parser.add_argument("image", nargs="?", default="camhal-sim:local")
    args = parser.parse_args()
    project = "camhal-prod-smoke-" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="camhal-prod-smoke-") as tmp_name:
        tmp = Path(tmp_name)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-noenc",
                        "-keyout", str(tmp / "idp.key"), "-out", str(tmp / "idp.crt"),
                        "-days", "1", "-subj", "/CN=oidc.test", "-addext", "subjectAltName=DNS:oidc.test",
                        "-addext", "basicConstraints=critical,CA:TRUE"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-noenc",
                        "-keyout", str(tmp / "privkey.pem"), "-out", str(tmp / "fullchain.pem"),
                        "-days", "1", "-subj", "/CN=localhost"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        settings = {
            "CAMHAL_PUBLIC_URL": "https://localhost:18443",
            "CAMHAL_OIDC_ISSUER": "https://oidc.test:4443",
            "CAMHAL_OIDC_AUDIENCE": "camhal-api",
            "CAMHAL_OIDC_JWKS_URL": "https://oidc.test:4443/keys",
            "CAMHAL_OIDC_CLIENT_ID": "camhal-web",
            "CAMHAL_OIDC_CLIENT_SECRET": "smoke-secret",
            "CAMHAL_OIDC_SCOPE": "openid email profile camhal-api",
            "CAMHAL_ALLOWED_EMAIL_DOMAIN": "example.com",
            "CAMHAL_COOKIE_SECRET": base64.urlsafe_b64encode(os.urandom(32)).decode(),
            "CAMHAL_AUTH_SUBNET": "172.30.252.0/24",
            "CAMHAL_PROXY_IP": "172.30.252.10",
        }
        env_file = tmp / "smoke.env"
        env_file.write_text("".join(f"{key}={value}\n" for key, value in settings.items()))

        config = yaml.safe_load((ROOT / "compose.production.yaml").read_text())
        app = config["services"]["camhal"]
        app.pop("build")
        app["image"] = args.image
        app["environment"]["SSL_CERT_FILE"] = "/certs/idp.crt"
        app["volumes"].append(f"{tmp}/idp.crt:/certs/idp.crt:ro")
        oauth = config["services"]["oauth2-proxy"]
        oauth["environment"]["SSL_CERT_FILE"] = "/certs/idp.crt"
        oauth["volumes"] = [f"{tmp}/idp.crt:/certs/idp.crt:ro"]
        nginx = config["services"]["nginx"]
        nginx["ports"] = ["127.0.0.1:18080:80", "127.0.0.1:18443:443"]
        nginx["volumes"] = [f"{ROOT}/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro",
                            f"{tmp}:/etc/nginx/tls:ro"]
        config["services"]["idp"] = {
            "image": args.image, "user": "0", "command": ["python", "/idp.py", "/certs"],
            "volumes": [f"{ROOT}/tests/oidc_fixture.py:/idp.py:ro", f"{tmp}:/certs:ro"],
            "ports": ["127.0.0.1:4443:4443"],
            "networks": {"auth-net": {"aliases": ["oidc.test"]},
                         "app-net": {"aliases": ["oidc.test"]}},
        }
        spec = tmp / "compose.yaml"
        spec.write_text(yaml.safe_dump(config))
        try:
            probe = subprocess.run(["docker", "compose", "version"], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            executable = ["docker", "compose"] if probe.returncode == 0 else ["docker-compose"]
        except FileNotFoundError:
            executable = ["docker-compose"]
        command = [*executable, "-p", project, "--env-file", str(env_file), "-f", str(spec)]
        try:
            subprocess.run([*command, "up", "-d"], check=True, stdout=subprocess.DEVNULL)
            for _ in range(30):
                try:
                    if curl("-o", "/dev/null", "-w", "%{http_code}",
                            "https://localhost:18443/api/catalog") == "401":
                        break
                except subprocess.CalledProcessError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Production Compose gateway did not become ready")

            cookies = tmp / "user-a.cookies"
            status = curl("-L", "--max-redirs", "10", "--resolve", "oidc.test:4443:127.0.0.1",
                          "-c", str(cookies), "-b", str(cookies), "-o", str(tmp / "home.html"),
                          "-w", "%{http_code}", "https://localhost:18443/")
            if status != "200":
                raise RuntimeError(f"Browser login returned {status}")
            if curl("-b", str(cookies), "-o", "/dev/null", "-w", "%{http_code}",
                    "https://localhost:18443/api/catalog") != "200":
                raise RuntimeError("Authenticated catalog request failed")
            created = json.loads(curl("-b", str(cookies), "-H", "Content-Type: application/json",
                                      "-d", '{"seed":7}',
                                      "https://localhost:18443/api/faults/F04/run"))
            run_id = created["id"]
            other = tmp / "user-b.cookies"
            other.write_text("oidc.test\tFALSE\t/\tFALSE\t0\tidp_user\tb\n")
            status = curl("-L", "--max-redirs", "10", "--resolve", "oidc.test:4443:127.0.0.1",
                          "-c", str(other), "-b", str(other), "-o", str(tmp / "other.html"),
                          "-w", "%{http_code}",
                          "https://localhost:18443/oauth2/start?rd=%2F%23%2Fhistory")
            if status != "200":
                raise RuntimeError(f"Second user login returned {status}")
            if curl("-b", str(other), "-o", "/dev/null", "-w", "%{http_code}",
                    f"https://localhost:18443/api/runs/{run_id}") != "404":
                raise RuntimeError("Second user could read another user's run")
            print("Production Compose smoke passed: startup, login, run creation, tenant isolation")
        finally:
            subprocess.run([*command, "down", "-v"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()

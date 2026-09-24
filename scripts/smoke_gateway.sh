#!/usr/bin/env bash
# Exercise the Nginx authentication boundary with stub auth and app servers.
set -euo pipefail

image="${1:-camhal-sim:local}"
tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/camhal-gateway.XXXXXX")
smoke_id=$(basename "$tmp_dir")
network_name="${smoke_id}-net"
auth_name="${smoke_id}-auth"
app_name="${smoke_id}-app"
nginx_name="${smoke_id}-nginx"
root_dir=$(cd "$(dirname "$0")/.." && pwd)

cleanup() {
  docker rm -f "$nginx_name" "$auth_name" "$app_name" >/dev/null 2>&1 || true
  docker network rm "$network_name" >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

cat > "$tmp_dir/stub.py" <<'PY'
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

mode = sys.argv[1]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if mode == "auth":
            if (self.headers.get("X-Forwarded-For") == "203.0.113.77" or
                    self.headers.get("X-Forwarded-Uri") == "/forged" or
                    self.headers.get("X-Forwarded-Host") == "forged.example"):
                self.send_response(400)
                self.end_headers()
                return
            if self.path.startswith("/oauth2/auth"):
                if self.headers.get("Authorization") or self.headers.get("X-Forwarded-Access-Token"):
                    self.send_response(400)
                    self.end_headers()
                    return
                authorized = "demo=ok" in self.headers.get("Cookie", "")
                self.send_response(202 if authorized else 401)
                if authorized:
                    self.send_header("X-Auth-Request-Access-Token", "verified-token")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
            return

        body = json.dumps({
            "authorization": self.headers.get("Authorization"),
            "forwarded_access_token": self.headers.get("X-Forwarded-Access-Token"),
            "forwarded_for": self.headers.get("X-Forwarded-For"),
            "forwarded_host": self.headers.get("X-Forwarded-Host"),
            "forwarded_uri": self.headers.get("X-Forwarded-Uri"),
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("0.0.0.0", 4180 if mode == "auth" else 8010), Handler).serve_forever()
PY

openssl req -x509 -newkey rsa:2048 -noenc -keyout "$tmp_dir/privkey.pem" \
  -out "$tmp_dir/fullchain.pem" -days 1 -subj /CN=localhost >/dev/null 2>&1

docker network create "$network_name" >/dev/null
docker run -d --name "$auth_name" --network "$network_name" \
  --network-alias oauth2-proxy -v "$tmp_dir/stub.py:/stub.py:ro" \
  "$image" python /stub.py auth >/dev/null
docker run -d --name "$app_name" --network "$network_name" \
  --network-alias camhal -v "$tmp_dir/stub.py:/stub.py:ro" \
  "$image" python /stub.py app >/dev/null
docker run -d --name "$nginx_name" --network "$network_name" -p 127.0.0.1::443 \
  -v "$root_dir/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  -v "$tmp_dir:/etc/nginx/tls:ro" nginx:1.30.4-alpine >/dev/null

address=$(docker port "$nginx_name" 443/tcp)
port="${address##*:}"
url="https://127.0.0.1:$port"
ready=false
for _ in {1..20}; do
  if [[ $(curl -sk --max-time 2 -o /dev/null -w '%{http_code}' "$url/api/catalog") == 401 ]]; then
    ready=true
    break
  fi
  sleep 1
done
[[ "$ready" == true ]] || { echo "Gateway did not become ready" >&2; exit 1; }

browser_status=$(curl -sk -o /dev/null -w '%{http_code}' "$url/")
auth_endpoint_status=$(curl -sk -o /dev/null -w '%{http_code}' "$url/oauth2/auth")
forged_status=$(curl -sk -o /dev/null -w '%{http_code}' \
  -H 'Authorization: Bearer forged-token' \
  -H 'X-Forwarded-For: 203.0.113.77' \
  -H 'X-Forwarded-Uri: /forged' \
  -H 'X-Forwarded-Host: forged.example' "$url/api/catalog")
response=$(curl -sk -H 'Cookie: demo=ok' \
  -H 'Authorization: Bearer forged-token' \
  -H 'X-Forwarded-Access-Token: forged-token' \
  -H 'X-Forwarded-For: 203.0.113.77' \
  -H 'X-Forwarded-Uri: /forged' \
  -H 'X-Forwarded-Host: forged.example' "$url/api/catalog")
oauth_status=$(curl -sk -o /dev/null -w '%{http_code}' \
  -H 'X-Forwarded-For: 203.0.113.77' \
  -H 'X-Forwarded-Uri: /forged' \
  -H 'X-Forwarded-Host: forged.example' "$url/oauth2/callback")

[[ "$browser_status" == 302 ]]
[[ "$auth_endpoint_status" == 404 ]]
[[ "$forged_status" == 401 ]]
[[ "$oauth_status" == 200 ]]
python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["authorization"] == "Bearer verified-token"; assert d["forwarded_access_token"] is None; assert d["forwarded_for"] and d["forwarded_for"] != "203.0.113.77"; assert d["forwarded_host"] != "forged.example"; assert d["forwarded_uri"] == "/api/catalog"' <<< "$response"
curl -sk "$url/oauth2/callback?code=secret-marker" >/dev/null
if docker logs "$nginx_name" 2>&1 | grep -q 'secret-marker'; then
  echo "OAuth callback query leaked into gateway logs" >&2
  exit 1
fi
echo "Gateway smoke passed: API 401, browser 302, forged headers stripped, token replaced, callback query omitted from logs"

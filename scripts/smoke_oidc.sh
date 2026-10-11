#!/usr/bin/env bash
# End-to-end browser login through Nginx, OAuth2 Proxy, the API, and a local OIDC issuer.
set -euo pipefail

image="${1:-camhal-sim:local}"
tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/camhal-oidc.XXXXXX")
smoke_id=$(basename "$tmp_dir")
network_name="${smoke_id}-net"
idp_name="${smoke_id}-idp"
redis_name="${smoke_id}-redis"
app_name="${smoke_id}-app"
auth_name="${smoke_id}-auth"
nginx_name="${smoke_id}-nginx"
root_dir=$(cd "$(dirname "$0")/.." && pwd)

cleanup() {
  docker rm -f "$nginx_name" "$auth_name" "$app_name" "$redis_name" "$idp_name" >/dev/null 2>&1 || true
  docker network rm "$network_name" >/dev/null 2>&1 || true
  rm -rf "$tmp_dir"
}
finish() {
  status=$?
  if [[ "$status" -ne 0 ]]; then
    if [[ -f "$tmp_dir/login-a.headers" ]]; then
      grep -Ei '^HTTP/|^location:' "$tmp_dir/login-a.headers" >&2 || true
    fi
    for name in "$idp_name" "$redis_name" "$app_name" "$auth_name" "$nginx_name"; do
      echo "--- $name ---" >&2
      docker logs "$name" 2>&1 | tail -20 >&2 || true
    done
  fi
  cleanup
}
trap finish EXIT

openssl req -x509 -newkey rsa:2048 -noenc -keyout "$tmp_dir/idp.key" \
  -out "$tmp_dir/idp.crt" -days 1 -subj /CN=oidc.test \
  -addext 'subjectAltName=DNS:oidc.test' \
  -addext 'basicConstraints=critical,CA:TRUE' >/dev/null 2>&1
openssl req -x509 -newkey rsa:2048 -noenc -keyout "$tmp_dir/privkey.pem" \
  -out "$tmp_dir/fullchain.pem" -days 1 -subj /CN=localhost >/dev/null 2>&1

docker network create "$network_name" >/dev/null
network_subnet=$(docker network inspect "$network_name" --format '{{(index .IPAM.Config 0).Subnet}}')
docker run -d --name "$idp_name" --network "$network_name" --network-alias oidc.test \
  -p 127.0.0.1:4443:4443 -v "$root_dir/tests/oidc_fixture.py:/idp.py:ro" \
  -v "$tmp_dir:/certs:ro" --user 0 "$image" python /idp.py /certs >/dev/null
docker run -d --name "$redis_name" --network "$network_name" --network-alias redis \
  redis:7.4-alpine redis-server --save '' --appendonly no >/dev/null
docker run -d --name "$app_name" --network "$network_name" --network-alias camhal \
  -e CAMHAL_ENV=production -e CAMHAL_AUTH_MODE=oidc \
  -e CAMHAL_OIDC_ISSUER=https://oidc.test:4443 -e CAMHAL_OIDC_AUDIENCE=camhal-api \
  -e CAMHAL_OIDC_JWKS_URL=https://oidc.test:4443/keys \
  -e SSL_CERT_FILE=/certs/idp.crt -v "$tmp_dir/idp.crt:/certs/idp.crt:ro" \
  "$image" >/dev/null
docker run -d --name "$auth_name" --network "$network_name" --network-alias oauth2-proxy \
  -e OAUTH2_PROXY_PROVIDER=oidc -e OAUTH2_PROXY_HTTP_ADDRESS=0.0.0.0:4180 \
  -e OAUTH2_PROXY_OIDC_ISSUER_URL=https://oidc.test:4443 \
  -e OAUTH2_PROXY_CLIENT_ID=camhal-web -e OAUTH2_PROXY_CLIENT_SECRET=smoke-secret \
  -e OAUTH2_PROXY_REDIRECT_URL=https://localhost:18443/oauth2/callback \
  -e OAUTH2_PROXY_SCOPE='openid email profile camhal-api' \
  -e OAUTH2_PROXY_EMAIL_DOMAINS=example.com \
  -e OAUTH2_PROXY_COOKIE_SECRET=0123456789abcdef0123456789abcdef \
  -e OAUTH2_PROXY_COOKIE_NAME=__Host-camhal_session \
  -e OAUTH2_PROXY_COOKIE_SECURE=true -e OAUTH2_PROXY_COOKIE_SAMESITE=lax \
  -e OAUTH2_PROXY_REVERSE_PROXY=true -e OAUTH2_PROXY_TRUSTED_PROXY_IPS="$network_subnet" \
  -e OAUTH2_PROXY_SKIP_PROVIDER_BUTTON=true -e OAUTH2_PROXY_SET_XAUTHREQUEST=true \
  -e OAUTH2_PROXY_PASS_ACCESS_TOKEN=true -e OAUTH2_PROXY_PASS_AUTHORIZATION_HEADER=false \
  -e OAUTH2_PROXY_PASS_BASIC_AUTH=false -e OAUTH2_PROXY_PASS_USER_HEADERS=false \
  -e OAUTH2_PROXY_REQUEST_LOGGING=false -e OAUTH2_PROXY_SESSION_STORE_TYPE=redis \
  -e OAUTH2_PROXY_REDIS_CONNECTION_URL=redis://redis:6379 \
  -e OAUTH2_PROXY_UPSTREAMS=static://200 \
  -e SSL_CERT_FILE=/certs/idp.crt -v "$tmp_dir/idp.crt:/certs/idp.crt:ro" \
  quay.io/oauth2-proxy/oauth2-proxy:v7.15.4 >/dev/null
docker run -d --name "$nginx_name" --network "$network_name" \
  -p 127.0.0.1:18443:443 \
  -v "$root_dir/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  -v "$tmp_dir:/etc/nginx/tls:ro" nginx:1.30.4-alpine >/dev/null

app_ready=false
for _ in {1..30}; do
  if docker exec "$app_name" python -c \
      'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8010/api/ready", timeout=2)' \
      >/dev/null 2>&1; then
    app_ready=true
    break
  fi
  sleep 1
done
[[ "$app_ready" == true ]] || { echo "App did not become ready" >&2; exit 1; }

ready=false
for _ in {1..30}; do
  if [[ $(curl -sk --noproxy '*' -o /dev/null -w '%{http_code}' \
      https://localhost:18443/api/catalog) == 401 ]]; then
    ready=true
    break
  fi
  sleep 1
done
[[ "$ready" == true ]] || { echo "OIDC gateway did not become ready" >&2; exit 1; }

curl -sSkL --noproxy '*' --max-redirs 10 \
  --resolve oidc.test:4443:127.0.0.1 \
  -c "$tmp_dir/user-a.cookies" -b "$tmp_dir/user-a.cookies" \
  -D "$tmp_dir/login-a.headers" -o "$tmp_dir/login-a.html" -w '%{http_code}' \
  https://localhost:18443/ > "$tmp_dir/login-a.status"
[[ $(cat "$tmp_dir/login-a.status") == 200 ]]
grep -qi "script-src 'self'" "$tmp_dir/login-a.headers"

catalog_status=$(curl -sk --noproxy '*' -o /dev/null -w '%{http_code}' \
  -b "$tmp_dir/user-a.cookies" https://localhost:18443/api/catalog)
[[ "$catalog_status" == 200 ]]
run_id=$(curl -sk --noproxy '*' -b "$tmp_dir/user-a.cookies" \
  -H 'Content-Type: application/json' -d '{"seed":7}' \
  https://localhost:18443/api/faults/F04/run \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -sk --noproxy '*' -b "$tmp_dir/user-a.cookies" \
  -D "$tmp_dir/report.headers" -o /dev/null \
  "https://localhost:18443/api/runs/$run_id/report.html"
grep -qi "script-src 'none'" "$tmp_dir/report.headers"

printf 'oidc.test\tFALSE\t/\tFALSE\t0\tidp_user\tb\n' > "$tmp_dir/user-b.cookies"
return_url='https://localhost:18443/#/history'
encoded_return=$(python3 -c 'import urllib.parse; print(urllib.parse.quote("/#/history", safe=""))')
curl -skL --noproxy '*' --max-redirs 10 \
  --resolve oidc.test:4443:127.0.0.1 \
  -c "$tmp_dir/user-b.cookies" -b "$tmp_dir/user-b.cookies" \
  -o "$tmp_dir/login-b.html" -w '%{http_code} %{url_effective}' \
  "https://localhost:18443/oauth2/start?rd=$encoded_return" > "$tmp_dir/login-b.result"
[[ $(cat "$tmp_dir/login-b.result") == "200 $return_url" ]]
other_status=$(curl -sk --noproxy '*' -o /dev/null -w '%{http_code}' \
  -b "$tmp_dir/user-b.cookies" "https://localhost:18443/api/runs/$run_id")
[[ "$other_status" == 404 ]]

echo "OIDC smoke passed: browser login, return route, API token verification, two-user isolation"

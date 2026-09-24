# 운영 배포 안내

현재 배포 단위는 SQLite 볼륨을 쓰는 **단일 인스턴스**다. `compose.yaml`은 로컬 전용으로
`127.0.0.1:8010`에 바인딩한다. 이 설정을 그대로 공개 네트워크에 연결하지 않는다.
컨테이너 이미지 자체는 기본적으로 운영 모드에서 시작하므로, OIDC 인증 설정 없이
단독 실행하면 서버가 시작되지 않는다. 로컬 Compose는 개발용 인증 모드를 명시한다.

## HTTPS와 브라우저 로그인 배포

`compose.production.yaml`은 Nginx(HTTPS) → OAuth2 Proxy(로그인 세션) → 앱의 경로를
제공한다. Redis에는 로그인 세션을 저장한다. 앱과 Redis는 호스트 포트를 열지 않는다.
앱은 JWKS 키를 조회해야 하므로 OIDC 공급자 주소로 나가는 HTTPS 연결이 필요하다.

1. OIDC 공급자에 웹 클라이언트를 등록하고 리디렉션 URI를
   `https://<도메인>/oauth2/callback`로 지정한다. 공급자가 **이 API용 audience가 들어간
   RS256 JWT 액세스 토큰**을 발급하도록 설정한다. 이 설정은 공급자별로 다르다.
2. `.env.production.example`을 `.env.production`으로 복사한 뒤 도메인, 발급자,
   audience, JWKS 주소, 클라이언트 ID·비밀값, API 토큰을 요청하는 scope,
   허용 이메일 도메인을 채운다. 쿠키 비밀값은
   `python3 -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'`
   로 새로 생성한다. `.env.production`은 Git에서 제외된다.
3. 도메인에 유효한 인증서와 키를 `deploy/tls/fullchain.pem`,
   `deploy/tls/privkey.pem`에 놓고 키 파일 접근 권한을 제한한다. Docker의
   `CAMHAL_AUTH_SUBNET`이 다른 네트워크와 겹치지 않는지 확인한다.
4. 서버에 Python 환경이 없다면 `python3 -m venv .venv`와
   `.venv/bin/pip install -r requirements-server.lock`을 실행한다.
   `.venv/bin/python scripts/preflight_production.py`로 예시 값·비밀값·TLS 인증서와
   개인 키·네트워크 설정을 검사한 뒤 아래 명령으로 배포한다.

   ```sh
   docker compose --env-file .env.production -f compose.production.yaml up -d --build
   ```

   `.env.production`과 TLS 개인 키는 `chmod 600`으로 보호한다. 이 검사는 인증서의 공개 신뢰 체인과
   실제 OIDC 발급 동작까지 확인하지는 않으므로 배포 후 브라우저에서 검증한다. Compose 플러그인이
   없으면 `docker-compose` 명령을 사용할 수 있다. 앱 컨테이너에서
   `docker compose --env-file .env.production -f compose.production.yaml exec camhal
   python -c 'import os,urllib.request; urllib.request.urlopen(os.environ["CAMHAL_OIDC_JWKS_URL"], timeout=5).read(1)'`
   을 실행해 JWKS 주소로 나가는 HTTPS 연결을 확인한다.
5. 로그인 후 `/api/catalog`가 200을 반환하는지, 비로그인 브라우저 요청이 로그인으로
   이동하는지, 비로그인 `/api/catalog`가 401을 반환하는지 확인한다. 서로 다른 두
   사용자로 기록의 조회·삭제 격리도 확인한다. 실제 공급자와 도메인을 연결해야 이
   종단 간 검증이 가능하다.

Nginx는 로그인 세션에서 얻은 액세스 토큰으로 업스트림 `Authorization`을 새로 만든다.
브라우저가 보낸 해당 헤더는 내부 인증 요청이나 앱으로 통과시키지 않는다. 앱은 이 토큰의 서명·발급자·
audience·만료를 다시 검증한다. OAuth2 Proxy의 [Nginx 연동 방식](https://oauth2-proxy.github.io/oauth2-proxy/configuration/integrations/nginx/)을
따르며, Redis가 재시작되면 세션은 사라져 사용자가 다시 로그인한다. 외부 TLS 인증서
갱신과 Redis 가용성·복구는 배포 환경에서 운영해야 한다.
`bash scripts/smoke_gateway.sh camhal-sim:local`은 임시 인증 서버로 비로그인 응답과
토큰 헤더 교체를 검증한다. 실제 OIDC 공급자를 통한 로그인은 위 배포 절차로 별도 확인한다.
`bash scripts/smoke_oidc.sh camhal-sim:local`은 테스트용 OIDC 발급자를 띄워 브라우저
로그인부터 API의 서명 검증과 두 사용자 간 기록 격리까지 확인한다. 테스트 발급자는
운영 서비스로 사용하지 않는다.
`python3 scripts/smoke_production_compose.py`는 실제 운영 Compose 설정을 임시로
복사해 로컬 인증서·발급자와 격리된 포트로 기동한 뒤 같은 로그인·사용자 격리를 검증한다.
화면에서 API가 401을 반환하면 현재 화면 경로를 보존하는 재로그인 링크를 표시한다.
Nginx 접근 로그에는 URL 쿼리와 인증 헤더를 기록하지 않으며 OAuth2 Proxy 요청 로그도
비활성화한다. 로그인 성공·실패 진단에는 OAuth2 Proxy 인증 로그와 앱의 요청 ID 로그를 쓴다.
앱 화면은 외부 스크립트·스타일·API 호출을 같은 출처로 제한하는 콘텐츠 보안 정책을 쓰며,
HTML 보고서는 스크립트 실행을 차단한다. Swagger UI(`/docs`)는 자체 인라인 초기화
스크립트를 사용하므로 별도 정책 적용 대상에서 제외하고 인증 게이트웨이로 보호한다.

## 인증 계약

운영 모드에서는 다음 환경 변수를 모두 지정해야 서버가 시작한다.

```text
CAMHAL_ENV=production
CAMHAL_AUTH_MODE=oidc
CAMHAL_OIDC_ISSUER=https://<발급자 식별자>
CAMHAL_OIDC_AUDIENCE=<이 API에 발급한 audience>
CAMHAL_OIDC_JWKS_URL=https://<발급자의 JWKS 주소>
```

OIDC를 지원하는 게이트웨이가 브라우저 로그인과 세션을 처리하고, 매 요청의 업스트림
`Authorization: Bearer <access-token>` 헤더에 **이 API용 서명된 JWT 액세스 토큰**을 전달해야 한다.
게이트웨이는 클라이언트가 보낸 `Authorization`을 그대로 통과시키지 말고 검증된 세션의
토큰으로 교체해야 한다. 앱은 JWKS 서명(RS256), 발급자, audience, 만료, subject를 검증한다.
검증된 subject별로 실행 결과의 저장·목록·상세·사건·이슈·보고서를 분리한다.
다른 사용자의 실행 ID를 알아도 404를 반환한다. `/api/health`와 `/api/ready`만
익명으로 접근할 수 있다.
사용자는 기록 화면 또는 `DELETE /api/runs/{id}`로 개별 기록을, `DELETE /api/runs`로
자신의 모든 기록을 서비스의 조회 대상에서 삭제할 수 있다. 다른 사용자의 ID로 삭제를
요청하면 404를 반환한다. SQLite의 `secure_delete`는 새 삭제 내용이 DB 페이지에
남는 것을 줄이지만, 기존 DB의 잔여 페이지·WAL 파일·이미 만든 백업 사본에는
삭제 전 기록이 남을 수 있다. 운영자는 백업 보관·만료 정책과 DB 파일 정리 절차를
별도로 정하고 삭제 요청 처리 시 이를 적용해야 한다.

TLS는 브라우저에서 게이트웨이까지의 모든 요청에 적용한다. 앱 포트는 게이트웨이만
접근할 수 있도록 네트워크에서 제한한다. 운영 모드는 게이트웨이 없이 브라우저에서
직접 사용하는 구성이 아니다.

`CAMHAL_AUTH_MODE=local`은 로컬 개발 전용이다. `CAMHAL_ENV=production`에서 local 모드로
시작하면 실패한다. 개발 모드에서 생성된 과거 기록은 `local` 소유이며 OIDC 사용자에게
자동 이전되지 않는다.

## 데이터와 운영

배포와 CI는 `requirements-server.lock`과 `web/package-lock.json`을 사용한다. 서버
의존성을 갱신할 때는 `requirements-server.txt`의 직접 의존성을 검토한 뒤 Python 3.12
환경에서 잠금 파일을 다시 생성하고, `pip check`, 전체 테스트, 프론트엔드 빌드,
컨테이너 빌드를 함께 확인한다. CI는 Python 잠금 파일과 npm 의존성의 알려진 취약점을
검사한다. 기본 브랜치에 반영되면 Dependabot이 npm·Dockerfile·Compose·GitHub Actions에
주간 갱신 PR을 만들며,
Python 잠금 파일의 갱신은 위 절차로 별도 검토한다.

- 데이터 파일은 `CAMHAL_DB_PATH`의 SQLite DB다. 컨테이너 기본 경로는
  `/var/lib/camhal/runs.sqlite3`이며 `camhal-data` 볼륨에 둔다. 새 실행의 큰 JSON과 HTML은
  압축 저장한다. 기존 비압축 기록은 계속 조회할 수 있다. DB 파일은 시작 시 소유자만
  읽고 쓸 수 있도록 `0600`으로 설정한다.
- 전체 최대 1,000건을 보관하며, 한 사용자는 최대 200건을 보관한다.
  한도를 넘기는 새 실행이 저장되면 해당 사용자의 가장 오래된 기록부터 삭제한다.
  전체 한도가 찼을 때도 새 실행을 요청한 사용자 자신의 오래된 기록만 삭제한다.
  그 기록으로 자리를 만들 수 없다면 `507`을 반환하고 다른 사용자의 기록은 유지한다.
  기존 DB에서 200건이 넘는 사용자의 기록은 그 사용자가 새 실행을 저장할 때 정리된다.
  이 보관 한도는 장기 보존을 보장하지 않으므로 운영 환경의 데이터 정책과 백업 주기를
  별도로 정해야 한다.
- 운영 중 백업은 `scripts/backup_runs.py`가 SQLite 백업 API로 일관된 스냅샷을 만들고
  `PRAGMA quick_check`로 검사한다. 기존 백업 파일은 덮어쓰지 않는다. 예:

  ```sh
  python3 scripts/backup_runs.py --db data/runs.sqlite3 /백업/경로/runs-YYYYMMDD.sqlite3
  ```

  운영 컨테이너에서는 다음처럼 호스트의 백업 디렉터리에 내보낸다.
  스크립트가 컨테이너에서 온라인 백업을 만든 뒤 호스트로 복사하고, 복사본의 무결성을
  다시 검사한 후 컨테이너 볼륨의 임시 사본을 삭제한다. 기존 파일은 덮어쓰지 않는다.

  ```sh
  mkdir -p backups
  python3 scripts/export_production_backup.py ./backups/runs-YYYYMMDD.sqlite3
  ```

  반복 백업에는 `scripts/scheduled_backup.py`를 스케줄러에서 실행한다. 백업 대상
  디렉터리와 `--retain-days`는 운영 정책에 맞게 명시해야 하며, 디렉터리가 없으면
  실패한다. 예를 들어 별도 저장소가 `/mnt/camhal-backups`에 마운트되고 30일 보관
  정책을 정했다면 다음 명령을 등록할 수 있다. 마운트가 필요한 환경에서는 실행 전에
  `mountpoint -q /mnt/camhal-backups`로 마운트 상태를 확인해야 한다.

  ```sh
  cd /srv/camera-hal-dev
  mountpoint -q /mnt/camhal-backups || exit 1
  python3 scripts/scheduled_backup.py /mnt/camhal-backups --retain-days 30
  ```

  스크립트는 중복 실행을 잠그고 새 백업의 무결성을 확인한 뒤에만 보관 기간이 지난
  파일을 정리한다. 정리 대상은 이 스크립트가 생성한 `runs-YYYYMMDDTHHMMSSZ-<UUID>.sqlite3`
  형식의 일반 파일뿐이다. 수동 백업과 심볼릭 링크는 건드리지 않는다. 백업 작업의
  성공 여부를 스케줄러에서 감시하고 실패 시 알림을 설정한다.

  복원할 때는 서버를 중지한 뒤 별도 볼륨에서 백업 파일을 열어 기록 조회를 확인하고
  교체한다. 호스트 백업도 같은 물리 디스크에만 남기지 말고 별도 저장소로 복제한다.
  백업 주기·보관 기간·복원 훈련은 운영 환경의 데이터 정책에 맞춰 정해야 한다.
  `python3 scripts/smoke_backup.py camhal-sim:local`은 임시 Compose 서비스에서
  백업 내보내기·복원 조회·볼륨 임시 파일 정리를 검증한다.
- 동시 실행은 프로세스당 4건, 사용자당 2건이다. 초과 요청에는 `503`과
  `Retry-After: 1`이 반환된다.
- 컨테이너 상태 검사는 `/api/ready`로 SQLite 조회 가능 여부와 DB 페이지 한도, DB 볼륨의
  남은 공간이 64 MiB 이상인지 확인한다. 용량이 빠르게 소진될 수 있으므로 볼륨 사용량과
  백업 저장소 사용량을 별도로 감시한다. `/api/health`는
  프로세스 생존 신호다. 응답의 `X-Request-ID`와 JSON 접근 로그의 `request_id`로
  요청을 연관 지을 수 있다. 접근 로그에는 Authorization 값과 요청 본문을 기록하지 않는다.
- Compose의 컨테이너 로그는 서비스별로 10 MiB 파일 3개까지 회전한다. 앱은 종료 시
  실행 중 요청을 마칠 수 있도록 30초의 종료 유예 시간을 둔다.
- SQLite 저장 공간이 부족하면 실행 API는 `507`을 반환하고 결과를 부분 저장하지 않는다.
  그 밖의 SQLite 오류는 `503`으로 응답하며 오류 코드와 요청 ID를 서버 로그에 남긴다.
  비교 실행의 두 결과는 하나의 트랜잭션으로 저장하며 저장이 실패하면 둘 다 남기지 않는다.
- 요청 본문은 32 KiB로 제한하며 초과 시 `413`을 반환한다. 게이트웨이에도 요청 크기와
  요청 빈도 제한을 설정한다.
- 다중 인스턴스 운영에는 외부 데이터 저장소와 실행 작업 큐가 필요하다.

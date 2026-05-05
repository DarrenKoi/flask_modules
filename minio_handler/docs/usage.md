# minio_handler 사용 가이드

## 패키지가 무엇인가

`minio_handler`는 MinIO / S3-compatible object storage를 다루는 작은 wrapper
입니다. 공식 `minio` Python SDK를 그대로 쓰되, 다음 세 가지를 편하게 만들어
줍니다.

- environment variable 기반 connection 설정
- bucket과 key prefix를 instance에 보관 (매 호출마다 반복하지 않아도 됨)
- 자주 쓰는 file 단위 CRUD와 presigned URL을 한 줄로 호출

S3 / MinIO에는 in-place update가 없습니다. 같은 key로 다시 `put`하면 그것이
update입니다. 이 wrapper도 별도의 update API를 두지 않습니다.

## 설치

`requirements.txt`에 이미 `minio`가 포함되어 있습니다.

```bash
pip install -r requirements.txt
```

## 설정값을 어디에 두는가

다음 세 가지 방법 중 편한 것을 골라 쓰면 됩니다. 우선순위는 위에서 아래로
**높음 → 낮음** 순입니다.

1. `MinioConfig(...)` / `MinioObject(...)` 호출 시 직접 넘기는 인자 (kwargs)
2. 환경 변수 (`MINIO_*`)
3. `minio_handler/minio_config.py` 안의 상수
4. 패키지 빌트인 기본값

즉 `minio_config.py`에 키를 넣어 두면 평소엔 그것이 쓰이고, 운영 환경에서는
환경 변수로 일시적으로 덮어쓸 수 있습니다.

### `minio_config.py` 사용

이 파일은 `.gitignore`에 등록되어 있어 절대 commit 되지 않습니다. 안심하고
키를 직접 넣어 둘 수 있습니다.

```python
# minio_handler/minio_config.py
ENDPOINT: str | None = "aistor-api.lake.skhynix.com"
ACCESS_KEY: str | None = "<여기에 access key>"
SECRET_KEY: str | None = "<여기에 secret key>"
SECURE: bool | None = False
REGION: str | None = None
CERT_CHECK: bool | None = True

BUCKET: str | None = "user"
PREFIX: str | None = "2067928/"
```

값을 채워 두면 application 코드는 인자 없이 한 줄로 끝납니다.

```python
from minio_handler import MinioObject

mo = MinioObject()              # ENDPOINT, KEY, BUCKET, PREFIX 모두 자동 적용
mo.put("hello.txt", b"hi")
print(mo.get("hello.txt"))
```

비워 두고 싶은 항목은 `None`으로 두면 됩니다. 그러면 그 항목만 환경 변수
또는 빌트인 기본값으로 떨어집니다.

> 새 clone에서는 `.gitignore` 때문에 이 파일이 존재하지 않습니다. 패키지는
> 파일이 없어도 정상 동작하므로, 필요할 때 직접 만들면 됩니다.

### 환경 변수 사용

`MinioConfig.from_env()`가 읽는 변수입니다.

| 변수 | 의미 | 기본값 |
| --- | --- | --- |
| `MINIO_ENDPOINT` | host[:port] (scheme 없이) | `localhost:9000` |
| `MINIO_ACCESS_KEY` | access key | `None` |
| `MINIO_SECRET_KEY` | secret key | `None` |
| `MINIO_SECURE` | HTTPS 사용 여부 | `False` |
| `MINIO_REGION` | region (대부분의 MinIO 배포에서는 불필요) | `None` |
| `MINIO_CERT_CHECK` | TLS 인증서 검증 | `True` |

> endpoint는 scheme 없이 host[:port]만 입력해야 합니다. `https://...` 같은
> 형식을 쓰면 SDK가 거부합니다. HTTPS 여부는 `secure` 파라미터로 조정합니다.

## 가장 짧은 사용 예

```python
from minio_handler import MinioConfig, MinioObject

config = MinioConfig(
    endpoint="aistor-api.lake.skhynix.com",
    access_key="<ACCESS_KEY>",
    secret_key="<SECRET_KEY>",
    secure=True,   # 사내 endpoint가 HTTPS면 True, HTTP면 False
)

mo = MinioObject(config=config, bucket="user", prefix="2067928/")

mo.put("hello.txt", b"hello minio")
data = mo.get("hello.txt")
print(data.decode())   # 'hello minio'
mo.delete("hello.txt")
```

`bucket="user"`, `prefix="2067928/"`로 만들었기 때문에 위에서 `"hello.txt"`라고
부른 object의 실제 key는 `2067928/hello.txt`입니다. `s3a://user/2067928/`
경로 아래에 파일이 만들어졌다고 보면 됩니다.

## 환경 변수로부터 만드는 경우

```python
from minio_handler import MinioObject, load_config

# 환경 변수: MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_SECURE
mo = MinioObject(config=load_config(), bucket="user", prefix="2067928/")
```

`MinioObject(...)`에 `config`를 명시하지 않으면 내부에서 자동으로
`load_config()`를 호출합니다. 즉 환경 변수만 잘 셋업해 두었다면 아래 한
줄로 끝납니다.

```python
mo = MinioObject(bucket="user", prefix="2067928/")
```

## Shared client를 직접 관리하는 경우

여러 서비스 instance에 같은 client를 주입하고 싶다면 `create_client`로
client를 한 번만 만들고 재사용합니다.

```python
from minio_handler import MinioObject, create_client, load_config

config = load_config()
client = create_client(config=config)

reports = MinioObject(client, config=config, bucket="user", prefix="2067928/reports/")
logs    = MinioObject(client, config=config, bucket="user", prefix="2067928/logs/")
```

같은 connection pool을 공유하므로 process 단위 resource 사용량이 줄어듭니다.

## 파일 쓰기 / 업데이트

### bytes를 직접 올리기

```python
mo.put("greeting.txt", b"hello")
mo.put("greeting.txt", b"hello again")   # 같은 key → 덮어쓰기 = update
```

`content_type`과 `metadata`도 같이 보낼 수 있습니다.

```python
mo.put(
    "report.json",
    b'{"score": 0.91}',
    content_type="application/json",
    metadata={"x-experiment-id": "exp-204"},
)
```

### 로컬 파일 업로드

```python
from pathlib import Path

mo.upload("inputs/train.csv", Path("/data/train.csv"))
```

`upload`는 큰 파일에 대해 SDK가 자동으로 multipart 분할을 해줍니다. 매우
큰 파일에서 `part_size`를 조정해 메모리 사용량과 속도를 튜닝할 수
있습니다.

```python
mo.upload(
    "datasets/big.parquet",
    Path("/data/big.parquet"),
    part_size=64 * 1024 * 1024,   # 64 MiB
)
```

### stream / file-like 업로드

길이를 알 수 없는 stream을 보낼 때는 `length=-1`과 `part_size`를 같이
넘깁니다.

```python
import io

stream = io.BytesIO(b"row1\nrow2\nrow3\n")
mo.put("logs/today.log", stream, length=-1, part_size=10 * 1024 * 1024)
```

## 파일 읽기

### bytes로 받기

```python
data: bytes = mo.get("report.json")
```

내부적으로 SDK가 돌려준 HTTP response를 다 읽고 connection을 안전하게
반환합니다. 호출자는 close를 신경 쓰지 않아도 됩니다.

### byte range만 읽기

큰 파일의 앞부분만 보고 싶을 때 유용합니다.

```python
head = mo.get("datasets/big.parquet", offset=0, length=4096)
```

### 로컬 파일로 다운로드

```python
from pathlib import Path

dst = mo.download("report.json", Path("/tmp/report.json"))
print(dst)   # PosixPath('/tmp/report.json')
```

목적지 디렉터리가 없으면 자동으로 만들어 줍니다.

## 메타데이터 조회와 존재 확인

```python
stat = mo.stat("report.json")
print(stat.size, stat.etag, stat.last_modified, stat.content_type)

if mo.exists("report.json"):
    print("있음")
```

`exists`는 NotFound 류의 S3 에러만 잡아서 `False`를 돌려주고, 그 외 권한
오류 등은 그대로 raise 합니다.

## 파일 삭제

```python
mo.delete("report.json")

# 여러 개 한꺼번에
errors = mo.delete_many(["a.txt", "b.txt", "c.txt"])
if errors:
    for err in errors:
        print("삭제 실패:", err)
```

`delete_many`는 한 번의 HTTP 요청으로 여러 object를 지웁니다. 반환값은
실패 항목들의 리스트입니다.

## 목록 조회

```python
for obj in mo.list():
    print(obj.object_name, obj.size)
```

prefix 아래 하위 경로를 제한하고 싶을 때:

```python
for obj in mo.list("logs", recursive=True):
    print(obj.object_name)
```

`recursive=False`로 두면 디렉터리 한 단계만 보여 주고, 그 아래는 prefix
하나로 묶여서 나옵니다 (S3의 common prefix 동작).

## Presigned URL

### 개념

Presigned URL은 access/secret key 없이도 짧은 시간 동안 특정 object 한 개에
대해 단일 동작(GET 또는 PUT)을 허용해 주는 임시 URL입니다. backend가 자기
key로 서명을 만들어 주면, URL을 가진 누구든 만료 전까지 그 동작을 수행할
수 있습니다.

- 만료 최대 7일 (AWS SigV4 제한, 604,800초)
- 만료 최소 1초
- URL 한 개 = method/bucket/key 한 쌍에 한정. listing이나 다른 key는 안 됨
- 이 wrapper의 default:
  - `presigned_get_url` → `timedelta(days=7)` (SigV4 최대값, 다운로드 링크 공유용)
  - `presigned_put_url` → `timedelta(minutes=20)` (업로드는 write 권한이라 짧게)
  더 길거나 짧게 쓰고 싶으면 호출 시 `expires=`를 명시하세요.

### 다운로드 URL 만들어 공유하기

```python
from datetime import timedelta

url = mo.presigned_get_url("reports/q1.pdf", expires=timedelta(minutes=15))
# https://aistor-api.lake.skhynix.com/user/2067928/reports/q1.pdf?X-Amz-...
```

링크를 메신저나 메일로 보내면 받는 사람은 그냥 브라우저로 열어서
다운로드합니다. 15분 뒤에는 같은 URL이 `403`을 돌려줍니다.

브라우저가 inline preview 대신 "다른 이름으로 저장" 다이얼로그를 띄우게
하려면 `response_headers`로 `Content-Disposition`을 강제할 수 있습니다.

```python
url = mo.presigned_get_url(
    "reports/q1.pdf",
    expires=timedelta(hours=1),
    response_headers={
        "response-content-disposition": 'attachment; filename="2026-Q1.pdf"',
    },
)
```

### Flask에서 다운로드 redirect로 활용

```python
# api/routes.py
from datetime import timedelta
from flask import redirect

from minio_handler import MinioObject, load_config

mo = MinioObject(config=load_config(), bucket="user", prefix="2067928/")

@api_bp.get("/files/<path:key>")
def download_file(key: str):
    if not mo.exists(key):
        return {"error": "not found"}, 404
    url = mo.presigned_get_url(key, expires=timedelta(minutes=5))
    return redirect(url, code=302)
```

이 방식의 장점:

- Flask 프로세스가 파일 바이트를 직접 흘려보내지 않음 → CPU/대역폭 절약
- 큰 파일도 메모리 부담이 없음
- MinIO가 객체 전송을 직접 처리하므로 더 잘 확장됨

### 브라우저 직접 업로드 (Presigned PUT)

backend는 짧게 살아 있는 PUT URL을 만들어 주고, 브라우저가 그 URL에 직접
파일을 올립니다. backend가 파일 바이트를 받아 다시 MinIO로 보내는 비용이
사라집니다.

```python
# backend
@api_bp.post("/files/<path:key>/upload-url")
def issue_upload_url(key: str):
    return {"url": mo.presigned_put_url(key, expires=timedelta(minutes=10))}
```

```js
// frontend
const { url } = await fetch(
  `/api/files/${encodeURIComponent(key)}/upload-url`,
  { method: "POST" },
).then((r) => r.json());

await fetch(url, { method: "PUT", body: file });   // file은 File / Blob
```

### Bucket과 prefix를 다르게 쓰고 싶을 때

`presigned_*` 메서드도 다른 메서드들과 동일하게 `bucket=` 인자로 default
bucket을 무시할 수 있습니다.

```python
url = mo.presigned_get_url(
    "shared/board.pdf",
    bucket="public",   # default bucket 대신 public bucket의 object를 서명
    expires=timedelta(minutes=30),
)
```

`prefix`는 항상 적용됩니다. 필요하다면 default prefix를 잠시 비웁니다.

```python
mo.use_prefix(None)
url = mo.presigned_get_url("misc/file.txt")
mo.use_prefix("2067928/")
```

### 7일을 넘기는 공유가 필요할 때

SigV4 spec이 7일을 hard limit으로 잡고 있어서, 한 URL을 그보다 오래 살게
만들 수는 없습니다. 그 대신 다음 세 가지 우회 전략 중 하나를 쓰면 됩니다.
실무에서는 1번이 압도적으로 많이 쓰입니다.

**1. 재발급 endpoint (가장 권장)**

URL을 직접 공유하지 말고, 매 요청마다 짧은 URL을 새로 만들어 redirect
하는 endpoint를 공유합니다. 사용자는 한 URL을 평생 bookmark할 수 있고,
실제 download URL은 매번 새로 서명됩니다.

```python
from datetime import timedelta
from flask import redirect

@api_bp.get("/share/<token>")
def share(token: str):
    # token을 DB에서 찾아 (key, expires_at, allowed_ips, ...) 정보를 얻고
    record = lookup_share(token)
    if record.is_expired():
        return {"error": "expired"}, 410
    url = mo.presigned_get_url(record.key, expires=timedelta(minutes=10))
    return redirect(url, code=302)
```

장점:

- 만료 / 권한 / IP 제한 / 다운로드 횟수 등 추가 정책을 자유롭게 얹음
- 키 관리는 backend 안에만 머무름
- 한 token을 revoke하기 쉬움 (DB record 한 줄만 끄면 됨)

**2. Bucket 또는 prefix를 public read로 정책 부여**

특정 prefix만 anonymous read를 허용하면 URL에 서명이 필요 없게 됩니다.
URL 만료 자체가 사라집니다.

```python
import json

policy = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"AWS": ["*"]},
            "Action": ["s3:GetObject"],
            "Resource": ["arn:aws:s3:::user/public/*"],
        }
    ],
}
mo.client.set_bucket_policy("user", json.dumps(policy))
```

이 방법은 키를 알면 누구나 읽을 수 있게 만듭니다. 실수로 민감한 파일을
이 prefix에 두면 즉시 노출되므로 일반 데이터에는 권하지 않습니다.

**3. Cron으로 주기적 재발급**

URL 자체가 어딘가에 박혀 있는 환경(이메일 본문, Slack 핀 메시지)에선
6일마다 새로 만들어 같은 자리에 갱신해 두는 cron job을 둡니다. 가장
fragile하므로 1번이 가능하면 1번을 쓰세요.

## Lifecycle: 오래된 파일 자동 삭제

S3 lifecycle은 "객체가 마지막으로 수정된 뒤 N일이 지나면 삭제"라는 규칙을
걸어 두는 기능입니다. MinIO도 이 규약을 그대로 지원합니다. 규칙을 한 번
걸어 두면 application 코드는 더 이상 삭제를 신경 쓰지 않아도 됩니다.

> **사내 환경 주의**: 우리 service account는 공유 `user` bucket의 lifecycle
> 정책을 *변경*할 권한이 없습니다. `set_expiration` / `set_lifecycle` /
> `clear_lifecycle` 호출은 `AccessDenied` 403을 돌려줍니다. 따라서 retention은
> 아래의 lifecycle wrapper 대신 **수동 cleanup job** (Airflow DAG 또는
> cron)으로 처리해야 합니다. 패턴은 `recipes.md`의 "정기 cleanup
> (lifecycle 대용)" 섹션을 보세요. `get_lifecycle()`은 read 권한이라 호출
> 가능하며, 우리에게 보이는 rule이 없으면 `None`을 돌려줍니다.

### 중요: 권한과 scope

- **lifecycle은 bucket 단위 설정입니다.** policy 문서는 `user` bucket 하나에
  통째로 붙고, 그 안에서 여러 `Rule`을 가지는 구조입니다.
- 따라서 정책을 *쓰려면* `s3:PutBucketLifecycle`(bucket admin) 권한이 필요
  합니다. 객체 read/write만 허용된 service account로는 403이 납니다.
- 우리처럼 `user` bucket의 `2067928/` prefix만 쓰는 환경이라면, 보통
  bucket 관리자가 우리 prefix용 rule을 한 번 등록해 주는 식으로 진행하고,
  application 쪽에서는 `get_lifecycle()`로 확인만 하면 됩니다.
- 직접 쓸 권한이 있더라도 **다른 tenant의 rule을 덮어쓰면 안 됩니다.**
  아래의 wrapper 메서드 `set_expiration`은 기존 rule들을 보존한 채 우리
  prefix의 rule만 갱신하도록 merge 동작을 합니다.

### 동작 원리

- rule은 prefix / tag / and-operator 중 하나로 적용 범위를 좁힘
- 기준 시각은 객체의 `last_modified` (즉 동일 key를 다시 `put`하면 다시
  카운팅이 시작됨)
- MinIO scanner가 주기적으로(기본 24시간) bucket을 훑으며 만료된 객체를
  제거. 즉 6개월이 되는 그 순간 즉시 사라지는 것이 아니라 다음 scan 때
  지워짐
- versioning이 켜진 bucket이면 `noncurrent_version_expiration`로 옛 버전
  관리 규칙도 따로 가능

### Wrapper API

`MinioObject`에 네 가지 메서드가 있습니다. 모두 `default_bucket`/
`default_prefix`(=`minio_config.py`의 `BUCKET`/`PREFIX`)를 자동으로 사용
합니다.

| 메서드 | 동작 |
| --- | --- |
| `mo.get_lifecycle()` | bucket의 현재 `LifecycleConfig` 또는 `None` |
| `mo.set_expiration(days)` | 우리 prefix용 "N일 뒤 삭제" rule을 추가/갱신 (다른 rule 보존) |
| `mo.set_lifecycle(config)` | raw `LifecycleConfig`로 **전체** 정책을 교체 (advanced) |
| `mo.clear_lifecycle()` | bucket의 모든 rule 삭제 (advanced) |

### 6개월 정책 만들기

```python
mo = MinioObject()                 # bucket=user, prefix=2067928/ from minio_config.py

mo.set_expiration(180)             # 우리 prefix만 180일 뒤 삭제
print(mo.get_lifecycle())          # 등록된 모든 rule 확인
```

`set_expiration`은 내부적으로 다음 단계를 거칩니다.

1. `get_bucket_lifecycle("user")`로 현재 정책 조회
2. 자동 생성된 rule_id (예: `expire-2067928-180d`) 와 같은 prefix slug를 가진
   기존 자동 rule을 제거 (즉, 같은 prefix에 대해 다른 일수로 다시 호출하면
   이전 자동 rule을 갈아끼움)
3. 새 rule을 추가
4. merge한 정책을 한 번에 PUT

다른 tenant의 rule(예: `1234567/` prefix용)은 모두 보존됩니다.

### 여러 prefix에 다른 정책

```python
mo.set_expiration(30,  prefix="2067928/logs/")
mo.set_expiration(180, prefix="2067928/reports/")
```

각 호출은 독립적인 rule을 만듭니다 (rule_id가 prefix slug에서 파생됨).
`mo.get_lifecycle()`로 결과를 확인할 수 있습니다.

### 현재 설정 확인

```python
config = mo.get_lifecycle()
if config is None:
    print("no lifecycle policy set")
else:
    for r in config.rules:
        print(r.rule_id, r.rule_filter.prefix, r.expiration.days)
```

### Advanced: raw config 직접 작성

복잡한 조건(여러 prefix, tag-based filter, `noncurrent_version_expiration`
등)이 필요하면 `LifecycleConfig`를 직접 만들어 `set_lifecycle`로 한 번에
밀어 넣을 수 있습니다. **이 방식은 기존 정책 전체를 교체**하므로 공유
bucket에서 사용할 때 주의하세요.

```python
from minio.commonconfig import ENABLED, Filter
from minio.lifecycleconfig import Expiration, LifecycleConfig, Rule

config = LifecycleConfig(
    rules=[
        Rule(
            rule_id="logs-30-days",
            rule_filter=Filter(prefix="2067928/logs/"),
            status=ENABLED,
            expiration=Expiration(days=30),
        ),
        Rule(
            rule_id="reports-180-days",
            rule_filter=Filter(prefix="2067928/reports/"),
            status=ENABLED,
            expiration=Expiration(days=180),
        ),
    ],
)
mo.set_lifecycle(config)
```

### 일자가 아니라 특정 날짜에 삭제

`Expiration(date=...)`를 쓰면 모든 매칭 객체가 그 날짜 이후 한꺼번에
삭제됩니다. 일회성 cleanup에 유용합니다. wrapper에는 이 단축 메서드가
없으므로 raw `LifecycleConfig`로 만들어 `set_lifecycle`을 씁니다.

```python
from datetime import datetime, timezone

Expiration(date=datetime(2026, 12, 31, tzinfo=timezone.utc))
```

### 자주 하는 실수

- **scan 주기를 모르고 즉시 삭제 기대.** 정확히 180일 0초가 되는 순간
  삭제되지 않습니다. 24시간 정도 지연 가능. SLA가 중요하면 lifecycle 대신
  `delete_many`를 cron에서 직접 돌리세요.
- **prefix slash 누락.** `prefix="2067928"`로 두면 `2067928foo/` 같은 다른
  객체에도 매칭됩니다. wrapper의 `set_expiration`은 자동으로 `/`를 붙이지만,
  raw `Filter(prefix=...)`를 직접 쓸 때는 직접 신경써야 합니다.
- **`last_modified` 갱신 함정.** 같은 key를 다시 `put`하면 lifecycle이
  새로 카운팅을 시작합니다. immutable한 archive면 의도대로지만, 자주
  덮어쓰는 파일이면 영원히 안 지워질 수 있습니다.
- **권한 부족.** `set_expiration`/`set_lifecycle`/`clear_lifecycle`은
  bucket admin 권한이 필요합니다. 객체 권한만 있는 service account로
  호출하면 `AccessDenied` 403이 납니다. 이 경우 bucket 관리자에게 우리
  prefix(`2067928/`)에 대한 rule 등록을 요청하고, application은
  `get_lifecycle()`로 적용 여부만 확인합니다.
- **`set_lifecycle`로 전체 덮어쓰기.** 공유 bucket에서 raw config를 그대로
  PUT하면 다른 tenant의 rule을 모두 날립니다. 단일 prefix에 대한 단순
  expiration이라면 항상 `set_expiration`을 쓰세요.

## 자주 하는 실수

### endpoint에 scheme을 붙임

```python
# ✗ 거절됨
MinioConfig(endpoint="https://aistor-api.lake.skhynix.com", ...)

# ✓
MinioConfig(endpoint="aistor-api.lake.skhynix.com", secure=True)
```

### secure 설정과 cluster 실제 protocol 불일치

`secure=True`인데 cluster가 HTTP만 받는다면 TLS handshake에서 즉시 실패
합니다. 반대로 `secure=False`인데 HTTPS 전용이면 401 또는 connection reset
이 납니다. 모르면 cluster 운영자에게 확인하는 편이 빠릅니다.

### prefix 슬래시 처리

`MinioBase`는 default prefix를 보관할 때 앞뒤 `/`를 제거합니다. 그래서
`prefix="2067928/"`, `prefix="/2067928"`, `prefix="2067928"` 셋이 모두 같은
의미입니다. key를 부를 때도 `mo.put("/foo")`, `mo.put("foo")` 둘 다 같은
key가 됩니다.

### S3 key를 OS path처럼 다루기

S3 key는 항상 `/`로 구분됩니다. Windows에서 코드가 동작해도 key는
`pathlib.Path`로 만들지 마세요. 로컬 파일 경로는 `Path`, S3 key는 그냥
문자열입니다.

```python
from pathlib import Path

local_file = Path("C:/data/train.csv")   # 로컬 → Path
mo.upload("inputs/train.csv", local_file)  # key는 문자열 그대로
```

### Presigned URL을 로그에 그대로 남김

서명이 들어 있는 URL은 만료 전까지 사용 가능한 한 번짜리 자격 증명입니다.
기본 정책은 다음과 같이 잡는 편이 안전합니다.

- 만료를 가능한 짧게 (수 분 ~ 1시간)
- 로그에는 URL 전체 대신 key와 만료 시각만 남기기
- 키가 노출됐을 가능성이 있다면 access key 자체를 회전

### 큰 stream을 length 없이 그대로 put

`length`를 모르는 stream을 보낼 때는 `length=-1`과 `part_size`를 반드시
같이 넘겨야 합니다. 그렇지 않으면 SDK가 size를 추정하지 못해서
`InvalidArgumentError`가 납니다.

```python
mo.put("logs/stream.log", some_stream, length=-1, part_size=10 * 1024 * 1024)
```

## 환경 변수만으로 사용하는 가장 짧은 형태

```bash
export MINIO_ENDPOINT=aistor-api.lake.skhynix.com
export MINIO_ACCESS_KEY=...
export MINIO_SECRET_KEY=...
export MINIO_SECURE=true
```

```python
from minio_handler import MinioObject

mo = MinioObject(bucket="user", prefix="2067928/")
mo.put("ping.txt", b"pong")
print(mo.get("ping.txt").decode())
mo.delete("ping.txt")
```

## `minio_config.py`만으로 사용하는 가장 짧은 형태

`minio_handler/minio_config.py`에 ENDPOINT / ACCESS_KEY / SECRET_KEY / BUCKET /
PREFIX 를 채워 두면 끝입니다.

```python
from minio_handler import MinioObject

mo = MinioObject()
mo.put("ping.txt", b"pong")
print(mo.get("ping.txt").decode())
mo.delete("ping.txt")
```

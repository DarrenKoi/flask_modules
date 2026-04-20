# 사용 가이드

## `ops_store`가 무엇인가

`ops_store`는 `opensearch-py` 위에 얇게 올린 wrapper입니다. OpenSearch의
핵심 개념인 index, mapping, query, raw response payload를 숨기지 않습니다.
대신 아래와 같은 편의성을 제공합니다.

- environment variable 기반의 일관된 config 로딩
- 역할별로 나뉜 작은 class 구성
- optional default index 처리
- OpenSearch 작업 결과를 위한 재사용 가능한 logging

즉, 이 패키지는 OpenSearch를 완전히 추상화하려는 목적이 아니라, 공식
client를 다루기 쉽게 만드는 convenience layer에 가깝습니다.

## 권장 startup 패턴

application startup 시점에 logging을 한 번만 설정하고, 이후에는 service
instance를 재사용하거나 shared client를 재사용하는 방식이 좋습니다.

```python
from ops_store import OSDoc, OSIndex, OSSearch, configure_logging

configure_logging(level="INFO")

index_service = OSIndex(index="articles")
doc_service = OSDoc(index="articles")
search_service = OSSearch(index="articles")
```

이 패턴이 동작하는 이유는 `OSBase`가 `config`나 `client`를 따로 넘기지
않아도 client를 자동 생성하기 때문입니다.

더 명시적으로 관리하고 싶다면 shared client를 직접 만든 뒤 여러 service에
주입하면 됩니다.

```python
from ops_store import OSDoc, OSIndex, OSSearch, create_client, load_config

config = load_config()
client = create_client(config=config)

index_service = OSIndex(client=client, config=config, index="articles")
doc_service = OSDoc(client=client, config=config, index="articles")
search_service = OSSearch(client=client, config=config, index="articles")
```

이 방식은 모든 service가 같은 client instance를 명확하게 공유해야 할 때 더
좋습니다.

## 기본 connection profile

service를 만들 때 `config`나 `client`를 직접 넘기지 않으면 `ops_store`는
environment variable에서 `OSConfig`를 만들고, 값이 없으면 아래 기본값을
사용합니다.

- `port=443`
- `use_ssl=True`
- `verify_certs=False`
- `ssl_show_warn=False`

즉 별도 override가 없으면 HTTPS endpoint의 `443` port를 기본으로 가정합니다.
TLS와 load balancer 뒤에 노출된 OpenSearch cluster에 잘 맞는 설정입니다.

### 예제: host만 바꿔서 기본값 사용

cluster가 이미 `443`에서 동작하고 있고, 내장 SSL 기본값을 그대로 쓰고
싶다면 host만 지정해도 충분합니다.

```bash
export OPENSEARCH_HOST=search.example.internal
export OPENSEARCH_USER=admin
export OPENSEARCH_PASSWORD=admin
```

```python
from ops_store import OSSearch

search_service = OSSearch(index="articles")
result = search_service.match("title", "hello")
```

### 예제: 기본 config를 명시적으로 생성

```python
from ops_store import OSConfig, create_client

config = OSConfig(
    host="search.example.internal",
    user="admin",
    password="admin",
)
client = create_client(config=config)
```

위 코드는 다음 설정으로 client를 만듭니다.

- `https://search.example.internal:443`
- `verify_certs=False`
- `ssl_show_warn=False`

### 예제: production에서 certificate 검증 강화

환경에 올바른 CA bundle이 있다면 SSL 기본값을 override해서 더 엄격하게
검증할 수 있습니다.

```python
from ops_store import OSConfig, OSDoc

config = OSConfig(
    host="search.example.internal",
    user="svc_search",
    password="secret",
    verify_certs=True,
    ssl_show_warn=True,
    ca_certs="/etc/ssl/certs/ca-bundle.pem",
)
doc_service = OSDoc(config=config, index="articles")
```

## Environment variables

`OSConfig.from_env()`와 `load_config()`는 아래 environment variable을
지원합니다.

- `OPENSEARCH_HOST`
- `OPENSEARCH_PORT`
- `OPENSEARCH_USER`
- `OPENSEARCH_PASSWORD`
- `OPENSEARCH_USE_SSL`
- `OPENSEARCH_VERIFY_CERTS`
- `OPENSEARCH_SSL_SHOW_WARN`
- `OPENSEARCH_CA_CERTS`
- `OPENSEARCH_BULK_CHUNK`
- `OPENSEARCH_TIMEOUT`
- `OPENSEARCH_MAX_RETRIES`
- `OPENSEARCH_RETRY_ON_TIMEOUT`
- `OPENSEARCH_HTTP_COMPRESS`
- `OPENSEARCH_LOG_LEVEL`
- `OPENSEARCH_LOG_DIR`

boolean 값은 `true`, `false`, `1`, `0`, `yes`, `no` 같은 일반적인 표현을
받아들입니다.

중요한 규칙:

- `OPENSEARCH_USER`를 설정하면 `OPENSEARCH_PASSWORD`도 반드시 있어야 합니다.
- `OPENSEARCH_PASSWORD`를 설정하면 `OPENSEARCH_USER`도 반드시 있어야 합니다.

그렇지 않으면 `OSConfig`가 `ValueError`를 발생시킵니다.

현재 기본 profile에 맞는 예시 environment:

```bash
export OPENSEARCH_HOST=search.example.internal
export OPENSEARCH_PORT=443
export OPENSEARCH_USE_SSL=true
export OPENSEARCH_VERIFY_CERTS=false
export OPENSEARCH_SSL_SHOW_WARN=false
export OPENSEARCH_USER=admin
export OPENSEARCH_PASSWORD=admin
```

## Index 작업

index lifecycle과 settings를 다룰 때는 `OSIndex`를 사용합니다.

```python
from ops_store import OSIndex

index_service = OSIndex(index="articles")

if not index_service.exists():
    index_service.create(
        mappings={
            "properties": {
                "title": {"type": "text"},
                "tags": {"type": "keyword"},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": 3,
                },
            }
        }
    )
```

shared client를 명시적으로 사용하는 예제:

```python
from ops_store import OSConfig, OSIndex, create_client

config = OSConfig(
    host="search.example.internal",
    user="admin",
    password="admin",
)
client = create_client(config=config)
index_service = OSIndex(client=client, config=config, index="articles")
```

알아두면 좋은 동작:

- `create()`는 별도 override가 없으면 shard, replica, refresh 기본값을
  채웁니다.
- `create()`는 새 index 생성 시 alias도 함께 붙일 수 있습니다.
- `update_settings()`는 OpenSearch API 형식에 맞게 settings를
  `{"index": settings}`로 감쌉니다.
- `get_aliases()`는 `index=`나 `default_index`가 없으면 전체 alias를
  반환합니다.
- `rollover()`는 alias 기반 index rollover를 위한 OpenSearch rollover API를
  감싼 wrapper입니다.

### Rollover에 맞는 index naming 규칙

data가 커질 가능성이 있다면 `articles` 같은 물리 index 이름에 계속 직접
쓰기보다는, 아래 패턴을 쓰는 것이 좋습니다.

- backing index: `articles-000001`, `articles-000002`, `articles-000003`
- write alias: `articles`
- app write 경로: 번호가 붙은 backing index가 아니라 alias로 쓰기

이 패턴이 중요한 이유는, 현재 write index 이름이 `-` 뒤에 숫자로 끝날 때
OpenSearch가 rollover 시 다음 index 이름을 자동 증가시킬 수 있기
때문입니다. 예를 들어 `articles-000001`은 rollover되면
`articles-000002`로 이어집니다.

권장 규칙:

- alias는 `articles`, `events`, `metrics`처럼 의미가 유지되는 stable 이름을
  사용
- backing index는 소문자 + 6자리 숫자 suffix 규칙을 유지
- `OSDoc`과 `OSSearch`는 `articles-000001`이 아니라 alias인 `articles`를
  바라보게 구성

### 첫 번째 write index bootstrap

첫 backing index를 만들고, alias를 write index로 지정합니다.

```python
from ops_store import OSIndex

index_service = OSIndex()
index_service.create(
    index="articles-000001",
    mappings={
        "properties": {
            "title": {"type": "text"},
            "published_at": {"type": "date"},
        }
    },
    aliases={
        "articles": {"is_write_index": True},
    },
)
```

그 다음부터는 alias를 통해 write하면 됩니다.

```python
from ops_store import OSDoc

doc_service = OSDoc(index="articles")
doc_service.index({"title": "hello"}, doc_id="post-1")
```

### index가 커졌을 때 rollover

rollover target은 backing index가 아니라 alias입니다.

```python
from ops_store import OSIndex

index_service = OSIndex(index="articles")
index_service.rollover(
    conditions={
        "max_docs": 1_000_000,
        "max_age": "7d",
        "max_primary_shard_size": "30gb",
    }
)
```

현재 write index가 `articles-000001` 같은 패턴을 따르고 있다면,
OpenSearch가 다음 index 이름을 자동으로 생성할 수 있습니다.

실제로 rollover하기 전에 조건만 확인하고 싶다면 `dry_run=True`를 먼저
사용하면 됩니다.

```python
result = index_service.rollover(
    conditions={"max_primary_shard_size": "30gb"},
    dry_run=True,
)
```

### 날짜가 들어간 rollover 이름

날짜까지 포함하고 싶다면 숫자 suffix를 맨 뒤에 유지하면 됩니다.

- `articles-2026.04.21-000001`
- `articles-2026.04.22-000002`

현재 write index가 `-<number>` 형식으로 끝나지 않으면 rollover 시
`new_index=...`를 명시적으로 넘겨야 합니다.

### rolled index에서의 search / write 패턴

rolled index 환경에서는 아래 규칙이 가장 단순합니다.

- write는 `OSDoc(index="articles")`
- read는 `OSSearch(index="articles")`

이렇게 하면 현재 write index가 `articles-000001`인지 `articles-000027`인지
application code는 신경 쓸 필요가 없습니다.

### ISM으로 자동 rollover를 돌리는 경우

manual API call이 아니라 ISM(Index State Management)으로 rollover를
자동화할 경우에도 naming pattern은 동일하게 유지하고, index settings에
rollover alias를 지정해야 합니다.

template settings 예시:

```json
{
  "settings": {
    "plugins.index_state_management.rollover_alias": "articles"
  }
}
```

ISM 기반 rollover에서는 backing index 이름이 `articles-000001`처럼
`^.*-\\d+$` 패턴을 만족해야 하고, 현재 index가 해당 alias의 write index여야
합니다.

## Document 작업

document 단위 read/write와 bulk write에는 `OSDoc`을 사용합니다.

```python
from ops_store import OSDoc

doc_service = OSDoc(index="articles")

doc_service.index(
    {"title": "Hello", "tags": ["flask"]},
    doc_id="post-1",
    refresh="wait_for",
)

doc_service.update("post-1", {"title": "Hello again"})
doc_service.upsert("post-2", {"title": "Created if missing"})
doc_service.delete("post-2")
```

package 기본값과 host override를 함께 쓰는 예제:

```python
from ops_store import OSDoc

doc_service = OSDoc(index="articles", host="search.example.internal")
doc_service.index({"title": "Hello"}, doc_id="post-1")
```

method별 동작:

- `index()`는 document 자체를 request body로 전송합니다.
- `update()`는 partial update를 위해 `{"doc": ...}` 형태를 사용합니다.
- `upsert()`는 `{"doc": ..., "doc_as_upsert": True}`를 사용합니다.
- `delete()`는 id 기준으로 document를 삭제합니다.

### Bulk write

이미 raw bulk action 구조를 알고 있다면 `bulk()`를 사용하면 됩니다.

```python
actions = [
    {"_op_type": "index", "_index": "articles", "_id": "1", "_source": {"title": "One"}},
    {"_op_type": "index", "_index": "articles", "_id": "2", "_source": {"title": "Two"}},
]

doc_service.bulk(actions, refresh=True)
```

일반 document dictionary 목록이 있고, 이 패키지가 bulk action을 만들어주길
원한다면 `bulk_index()`를 사용합니다.

```python
documents = [
    {"id": "1", "title": "One"},
    {"id": "2", "title": "Two"},
]

doc_service.bulk_index(documents, id_field="id", refresh=True)
```

이 records가 pandas나 NumPy 값에서 왔다면, OpenSearch로 들어가기 전에
JSON-safe payload로 바꾸기 위해 `normalize=True`를 켜는 것이 안전합니다.

```python
documents = df.to_dict(orient="records")
doc_service.bulk_index(documents, id_field="id", normalize=True, refresh=True)
```

중요한 점:

- `bulk_index()`는 내부에서 `document_count=len(documents)`를 기록하므로,
  generator가 아니라 list나 tuple 같은 sequence를 넘겨야 합니다.
- `normalize=True`는 `NaN`, `NaT`, `Timestamp`, `numpy.int64`,
  `numpy.float64`, `Decimal` 같은 JSON 비호환 값이 섞인 문서에 안전한
  모드입니다.

### Pandas DataFrame bulk indexing

source data가 pandas `DataFrame`이라면, 큰 데이터에서는
`df.to_dict(orient="records")`를 직접 만들기보다
`bulk_index_dataframe()`을 쓰는 편이 낫습니다.

```python
import pandas as pd

from ops_store import OSDoc

df = pd.DataFrame(
    [
        {"doc_id": 101, "title": "one", "score": 1.5},
        {"doc_id": 102, "title": "two", "score": None},
    ]
)

doc_service = OSDoc(index="articles")
doc_service.bulk_index_dataframe(df, id_field="doc_id", refresh=True)
```

이 method는 `itertuples()`로 row를 stream 처리하고, 각 row를 bulk action으로
만들기 전에 normalization을 수행합니다. 그래서 큰 DataFrame 전체를 한 번에
list of dicts로 바꾸는 추가 메모리 비용을 피할 수 있습니다.

row index 자체가 document identifier라면 DataFrame index를 OpenSearch `_id`로
사용할 수 있습니다.

```python
df = df.set_index("article_key")
doc_service.bulk_index_dataframe(df, id_from_index=True)
```

같은 호출에서 `id_field`와 `id_from_index=True`를 동시에 쓰면 안 됩니다.

### pandas 친화적인 type conversion 규칙

`bulk_index_dataframe()`은 항상 normalization을 수행합니다.
`bulk_index(..., normalize=True)`도 같은 규칙을 사용합니다.

- `NaN`, `NaT`, `None`, `pandas.NA`는 `None`으로 변환
- `numpy` scalar type은 Python 기본형 `int`, `float`, `bool` 등으로 변환
- `datetime`, `date`, `time`, pandas timestamp는 ISO 8601 string으로 변환
- `timedelta`는 총 초(second) 숫자로 변환
- `Decimal`은 `float`로 변환
- nested dictionary와 list는 재귀적으로 normalization
- mapping key는 string으로 변환

다른 표현 방식이 필요하다면 bulk helper를 호출하기 전에 DataFrame column을
직접 변환하는 것이 맞습니다. 특히 아래 경우가 중요합니다.

- `Decimal` money 값에서 문자열 정밀도가 중요한 경우
- duration을 초(second)가 아니라 string으로 저장하고 싶은 경우
- custom Python class가 object column에 들어 있는 경우
- MultiIndex column을 쓰고 있다면 먼저 plain string column name으로
  flatten하는 경우

### 예제: bulk indexing 전에 DataFrame 정리

```python
import pandas as pd

from ops_store import OSDoc

df = pd.DataFrame(
    [
        {
            "doc_id": 1,
            "title": "hello",
            "created_at": pd.Timestamp("2024-04-21T10:30:00Z"),
            "price": 12.5,
        }
    ]
)

df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
df["title"] = df["title"].fillna("")

doc_service = OSDoc(index="articles")
doc_service.bulk_index_dataframe(df, id_field="doc_id", refresh=True)
```

### 예제: 단일 record만 직접 normalize

문서 하나만 있고, 같은 conversion 규칙을 그대로 적용하고 싶다면
`normalize_document()`를 사용하면 됩니다.

```python
from ops_store import normalize_document

document = normalize_document(
    {
        "created_at": df.iloc[0]["created_at"],
        "score": df.iloc[0]["score"],
    }
)
```

## Search 작업

작고 명시적인 query helper가 필요할 때는 `OSSearch`를 사용합니다.

```python
from ops_store import OSSearch

search_service = OSSearch(index="articles")

search_service.match("title", "flask", size=5)
search_service.term("status", "published")
search_service.multi_match("vector search", ["title", "body"])
```

명시적인 config object를 넘기는 예제:

```python
from ops_store import OSConfig, OSSearch

config = OSConfig(
    host="search.example.internal",
    user="admin",
    password="admin",
)
search_service = OSSearch(config=config, index="articles")
hits = search_service.match("title", "flask")
```

Boolean query:

```python
search_service.bool(
    must=[{"match": {"title": "flask"}}],
    filter=[{"term": {"status": "published"}}],
    size=20,
)
```

Vector search:

```python
search_service.knn(
    field="embedding",
    vector=[0.1, 0.2, 0.3],
    k=5,
    size=5,
)
```

Hybrid search:

```python
search_service.hybrid(
    query="flask tutorial",
    text_field="title",
    vector_field="embedding",
    vector=[0.1, 0.2, 0.3],
    k=5,
    size=10,
)
```

중요한 점:

- `hybrid()`는 `match`와 `knn` clause를 `should`로 묶은 단순한 boolean query
  입니다. 더 고급 ranking/fusion pipeline을 대신하는 것은 아닙니다.

## Logging 동작

이 패키지는 OpenSearch response 전체가 아니라 요약(summary) 형태를 로그로
남깁니다.

권장 설정:

```python
from ops_store import configure_logging

configure_logging(level="INFO")
```

`configure_logging()` 이후 기본 동작:

- logger name은 `opensearch` namespace로 시작
- 로그는 parent logger로 propagate
- 기본적으로 file handler가 추가됨
- 로그 파일은 `logs/opensearch` 아래에 기록됨
- 각 process가 `opensearch.<pid>.log` 같은 자기 파일에 기록

script에서 콘솔 출력도 직접 보고 싶다면 `add_handler=True`를 사용하면 됩니다.

```python
configure_logging(level="DEBUG", add_handler=True, propagate=False)
```

## 올바른 사용 규칙

이 패키지를 안정적으로 쓰기 위해 가장 중요한 규칙은 아래와 같습니다.

- logging은 request handler마다 반복 설정하지 말고 startup 시점에 한 번만
  설정
- 가능하면 매 호출마다 새 service/client를 만들지 말고 재사용
- 대부분의 작업이 같은 index를 대상으로 한다면 service 생성 시
  default index를 지정
- default index를 지정하지 않았다면 항상 `index=...`를 넘겨서
  `ValueError`를 피하기
- package 기본값을 쓰는 경우 요청이 `9200`이 아니라 HTTPS `443`로 간다는
  점을 기억
- `bulk()`는 prebuilt action용, `bulk_index()`는 일반 document dictionary용
  으로 구분해서 사용
- `knn()`이나 `hybrid()` 호출 전에 vector field mapping이 올바르게 되어 있는지
  확인
- 테스트에서는 live OpenSearch cluster 대신 mocked `client`를 주입
- 이미 `client=...`를 넘긴 경우 client override kwargs를 같이 넘기지 말기.
  `OSBase`가 이 조합을 거부함

## Flask service에서 쓰는 예제

이 repository의 Flask app은 현재 health route만 제공하지만, Flask handler에서
`ops_store`를 사용할 때도 route는 얇게 유지하고 OpenSearch 로직은 service
class로 넘기는 편이 좋습니다.

```python
from flask import jsonify
from ops_store import OSDoc, configure_logging

configure_logging(level="INFO")
doc_service = OSDoc(index="articles")


def get_article(article_id: str):
    result = doc_service.get(article_id)
    return jsonify(result)
```

이렇게 하면 Flask는 HTTP를 담당하고, `ops_store`는 OpenSearch 관련 동작을
담당하게 되어 역할이 깔끔하게 나뉩니다.

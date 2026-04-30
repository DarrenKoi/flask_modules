# 사용 가이드

## `ops_store`가 무엇인가

`ops_store`는 `opensearch-py` 위에 얇게 올린 wrapper입니다. OpenSearch의
핵심 개념인 index, mapping, query, raw response payload를 숨기지 않습니다.
대신 아래와 같은 편의성을 제공합니다.

- environment variable 기반의 일관된 config 로딩
- 역할별로 나뉜 작은 class 구성
- optional default index 처리

즉, 이 패키지는 OpenSearch를 완전히 추상화하려는 목적이 아니라, 공식
client를 다루기 쉽게 만드는 convenience layer에 가깝습니다.

## 권장 startup 패턴

application startup 시점에 service instance나 shared client를 한 번 만들고
이후에는 재사용하는 방식이 좋습니다.

```python
from ops_store import OSDoc, OSIndex, OSSearch

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

## host와 http auth를 넘기는 여러 방법

OpenSearch cluster 주소(`host`, `port`, `use_ssl`)와 인증 정보(`user`,
`password`)를 service나 client에 전달하는 방식은 여러 가지가 있습니다. 모든
방식은 같은 precedence rule을 따릅니다.

- config가 없으면 → environment variable이 기본값을 제공하고, 그 위에
  keyword override가 덮어씁니다 (`load_config(**overrides)`).
- config가 있으면 → keyword override가 `OSConfig` 위에 덮어씁니다
  (`dataclasses.replace(config, **overrides)`).

즉 host와 http auth는 아래 여섯 가지 방식 중 어디에 넣어도 동일한 규칙으로
반영됩니다. 상황에 맞게 고르면 됩니다.

### 1. Environment variable만 사용

startup 전에 `OPENSEARCH_*`를 export해 두면 service는 argument를 거의
넘기지 않아도 됩니다.

```bash
export OPENSEARCH_HOST=search.example.internal
export OPENSEARCH_USER=admin
export OPENSEARCH_PASSWORD=admin
```

```python
from ops_store import OSIndex

index_service = OSIndex(index="articles")
```

deploy 환경마다 host나 credential이 달라져야 하고, code에 secret을 남기고
싶지 않을 때 가장 좋습니다.

### 2. service에 keyword argument 직접 전달

`OSDoc`, `OSIndex`, `OSSearch`는 `OSConfig` field를 그대로 keyword argument로
받습니다. 내부적으로 `load_config(**overrides)`가 호출되어 env 위에
override됩니다.

```python
from ops_store import OSIndex

index_service = OSIndex(
    index="articles",
    host="search.example.internal",
    user="admin",
    password="admin",
)
```

한두 값만 빠르게 바꾸거나, script와 test에서 가장 간편합니다.

### 3. `OSConfig`를 만들어 `config=`로 전달

host와 credential을 code에서 명시적으로 관리하고, 같은 설정을 여러 service에
재사용하고 싶을 때 사용합니다.

```python
from ops_store import OSConfig, OSDoc, OSIndex, OSSearch

config = OSConfig(
    host="search.example.internal",
    user="admin",
    password="admin",
)

index_service = OSIndex(config=config, index="articles")
doc_service = OSDoc(config=config, index="articles")
search_service = OSSearch(config=config, index="articles")
```

이 방식에서는 service마다 내부적으로 별도 client가 생성됩니다. 같은 client
instance를 공유해야 한다면 방식 5를 사용하세요.

### 4. `OSConfig` + keyword override 조합

기존 `OSConfig`는 그대로 두고 특정 값만 바꾸고 싶을 때는 두 가지를 함께
넘기면 됩니다. `dataclasses.replace(config, **overrides)`로 합쳐집니다.

```python
from ops_store import OSConfig, OSIndex

base_config = OSConfig(
    host="search.example.internal",
    user="admin",
    password="admin",
)

index_service = OSIndex(
    config=base_config,
    index="articles",
    timeout=10,
    max_retries=5,
)
```

base config는 공유하되 service별로 timeout 같은 tuning만 달리 주고 싶을 때
적합합니다.

### 5. `create_client()`로 shared client를 만들어 주입

여러 service가 같은 connection pool과 client instance를 명시적으로 공유해야
한다면 먼저 client를 만든 뒤 `client=`로 주입합니다.

```python
from ops_store import OSConfig, OSDoc, OSIndex, OSSearch, create_client

config = OSConfig(
    host="search.example.internal",
    user="admin",
    password="admin",
)
client = create_client(config=config)

index_service = OSIndex(client=client, config=config, index="articles")
doc_service = OSDoc(client=client, config=config, index="articles")
search_service = OSSearch(client=client, config=config, index="articles")
```

주의: 이미 `client=...`를 넘긴 경우에는 `host`, `user`, `password` 같은
client override keyword를 함께 넘길 수 없습니다. 이미 만들어진 client에
나중에 값을 덮어쓸 방법이 없기 때문에 `OSBase`는 이 조합을 `ValueError`로
거부합니다.

### 6. `create_client()`에 keyword만 전달

`OSConfig`를 만들지 않고도 `create_client()`에 host와 auth를 바로 넘길 수
있습니다. 내부적으로 `load_config(host=..., user=..., ...)`가 호출되어 env
위에 override됩니다.

```python
from ops_store import create_client, OSIndex

client = create_client(
    host="search.example.internal",
    user="admin",
    password="admin",
    timeout=10,
)

index_service = OSIndex(client=client, index="articles")
```

service 없이 raw client만 잠깐 필요할 때나, client만 먼저 만든 뒤 여러
service에 주입하고 싶을 때 적합합니다.

### 어떤 방식을 고를까

- deploy 환경별로 값이 달라져야 한다 → **방식 1** (env 전용)
- 한두 값만 code에서 바꾸는 간단한 script나 test → **방식 2** (service kwargs)
- 같은 설정을 여러 service에 명시적으로 재사용 → **방식 3**
- base config는 공유하되 일부 값만 tuning → **방식 4**
- 여러 service가 같은 client instance를 공유 → **방식 5**
- service 없이 raw client만 필요 → **방식 6**

## `create_client()`를 어떻게 쓰나

`create_client()`는 세 가지 방식으로 이해하면 충분합니다. class 이름은
`OSconfig`가 아니라 `OSConfig`입니다.

### 1. 아무 argument 없이 호출

environment variable과 기본값만으로 client를 만들고 싶다면 가장 단순하게
호출하면 됩니다.

```python
from ops_store import create_client

client = create_client()
```

이 경우 내부적으로 `load_config()`가 호출되고, `OPENSEARCH_*` environment
variable이 적용됩니다. environment variable이 없으면 `OSConfig`의 기본값을
사용합니다.

### 2. `OSConfig`를 직접 만든 뒤 `config=`로 전달

host, auth, SSL 옵션을 코드에서 명시적으로 관리하고 싶다면 `OSConfig`를 만든
다음 `config=config`로 넘기면 됩니다.

```python
from ops_store import OSConfig, create_client

config = OSConfig(
    host="search.example.internal",
    user="admin",
    password="admin",
    verify_certs=True,
)
client = create_client(config=config)
```

즉, `config=OSConfig(...)`는 필수가 아닙니다. 하지만 다음 경우에는 이 방식이
더 명확합니다.

- application startup 시점에 config를 한 번만 만들고 재사용하고 싶을 때
- 같은 config를 여러 service와 shared client에 같이 넘기고 싶을 때
- environment variable 대신 Python code에서 값을 명시하고 싶을 때

### 3. `config` 없이 keyword override만 전달

한두 개 값만 빠르게 바꾸고 싶다면 `create_client()`에 keyword argument를 바로
줄 수도 있습니다.

```python
from ops_store import create_client

client = create_client(
    host="search.example.internal",
    user="admin",
    password="admin",
    timeout=10,
)
```

이 경우에도 내부적으로는 `load_config(host=..., user=..., ...)`처럼 동작하므로
environment variable 위에 전달한 값이 override됩니다.

### 이미 `config`가 있는데 일부 값만 바꾸고 싶은 경우

`config`와 keyword override를 함께 주면 override 값이 기존 `config` 위에
덮어써집니다.

```python
from ops_store import OSConfig, create_client

config = OSConfig(
    host="search.example.internal",
    user="admin",
    password="admin",
)
client = create_client(config=config, timeout=10, max_retries=5)
```

### service를 쓸 때는 `create_client()`가 항상 필요한가

그렇지는 않습니다. `OSDoc`, `OSIndex`, `OSSearch`는 `config`만 받아도 내부에서
자동으로 client를 만듭니다.

```python
from ops_store import OSConfig, OSIndex

config = OSConfig(
    host="search.example.internal",
    user="admin",
    password="admin",
)
index_service = OSIndex(config=config, index="articles")
```

직접 `create_client()`를 호출하는 편이 더 좋은 경우는 shared client를 여러
service에 명시적으로 주입하고 싶을 때입니다.

## Connection 확인

`ops_store`는 OpenSearch client를 완전히 숨기지 않는 얇은 wrapper이므로,
connection 확인도 raw client method를 그대로 사용하는 방식이 가장
직접적입니다. 현재는 별도의 `check_connection()` helper를 두고 있지
않습니다.

가장 단순한 확인 방법은 `create_client()`로 만든 client에서 `ping()`을
호출하는 것입니다.

```python
from ops_store import create_client

client = create_client()
is_connected = client.ping()
```

이미 service instance를 만들었다면 내부 client를 그대로 사용할 수 있습니다.

```python
from ops_store import OSIndex

index_service = OSIndex(index="articles")
is_connected = index_service.client.ping()
```

단순한 reachability만 확인하는 것이 아니라 cluster metadata도 함께 보고
싶다면 `ping()` 대신 `info()`를 호출하면 됩니다.

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

- `exists()`는 기본적으로 concrete index와 alias를 모두 존재하는 것으로
  간주합니다. 물리 index만 확인하고 싶다면 `include_aliases=False`를
  사용하세요.
- `alias_exists()`는 alias namespace만 따로 확인합니다.
- `describe()`는 이름이 실제 index인지 alias인지, 어떤 backing index로
  연결되는지, 어떤 alias가 붙어 있는지, rollover 형태인지 한 번에
  요약해 줍니다.
- `create()`는 별도 override가 없으면 shard, replica, refresh 기본값을
  채웁니다.
- `recreate_index(index, shards=1, replica=0, mappings=None, aliases=None)`은
  기존 index가 있으면 삭제한 뒤 지정한 shard/replica 설정으로 다시 만들고,
  선택적으로 mapping과 alias도 함께 적용합니다.
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

### 테스트나 초기화 스크립트에서 index를 다시 만들기

기존 index를 지우고 shard/replica 수를 다시 지정해서 만들고 싶다면
`recreate_index()`를 사용할 수 있습니다.

```python
from ops_store import OSIndex

index_service = OSIndex()
index_service.recreate_index(
    "articles",
    shards=3,
    replica=1,
    mappings={
        "properties": {
            "title": {"type": "text"},
            "published_at": {"type": "date"},
        }
    },
    aliases={
        "articles-read": {},
        "articles-write": {"is_write_index": True},
    },
)
```

이 helper는 아래 순서로 동작합니다.

- 대상 index가 존재하면 먼저 삭제 (`delete()` 경유)
- `number_of_shards=shards`
- `number_of_replicas=replica`

중요한 점은 `recreate_index()`가 alias만 있는 경우를 기존 물리 index로
오해하지 않는다는 것입니다. 내부적으로는 concrete index 존재 여부만 보고
삭제 여부를 결정합니다.

- `mappings`가 있으면 새 index에 포함
- `aliases`가 있으면 새 index에 포함
- `refresh_interval="30s"`로 새 index 생성 (`create()` 경유)

```python
index_service.recreate_index(
    "articles",
    shards=3,
    replica=1,
    mappings={"properties": {"title": {"type": "text"}}},
    aliases={"articles-live": {}},
)
```

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

필요하면 search 시점에 concrete index로 직접 override할 수도 있습니다.

```python
from ops_store import OSSearch

search_service = OSSearch(index="articles")

alias_result = search_service.match("title", "flask")
backing_result = search_service.match(
    "title",
    "flask",
    index="articles-000002",
)
```

즉 `OSSearch`는 alias 이름을 기본값으로 써도 되고, 호출마다 특정 backing
index 이름으로 바꿔도 됩니다.

### alias, backing index, rollover 구성 확인하기

현재 이름이 실제 index인지 alias인지, rollover 형태인지 한 번에 보고
싶다면 `describe()`를 사용하면 됩니다.

```python
from ops_store import OSIndex

index_service = OSIndex()

summary = index_service.describe("articles")
```

예를 들어 alias 기반 rollover 구성이면 아래처럼 요약됩니다.

```python
{
    "name": "articles",
    "resource_type": "alias",
    "backing_indices": ["articles-000001", "articles-000002"],
    "aliases": {
        "articles": {
            "backing_indices": ["articles-000001", "articles-000002"],
            "write_index": "articles-000002",
        }
    },
    "searchable_names": [
        "articles",
        "articles-000001",
        "articles-000002",
    ],
    "rollover": {
        "alias": "articles",
        "write_index": "articles-000002",
        "ready": True,
        "uses_numbered_suffix": True,
    },
}
```

`include_metadata=True`를 주면 raw `indices.get(...)` 결과도 같이 포함되므로,
settings과 mappings까지 한 번에 확인할 수 있습니다.

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
doc_exists = doc_service.exists_many(["post-1", "post-2", "post-3"])
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
- `exists_many()`는 id list를 `_mget`으로 한 번에 확인하고 `{id: bool}`을
  반환합니다.
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

`_id`는 `id_field`로 명시한 column에서만 가져옵니다. 해당 column 값은
unique여야 하며, normalization 이후에도 `None`이 아닌 값이 있어야 action에
`_id`가 붙습니다.

### `op_type`으로 insert semantic 선택

`op_type` parameter로 bulk action의 OpenSearch semantic을 선택할 수
있습니다.

- `op_type=None` (default): action에 `_op_type`을 넣지 않습니다. OpenSearch
  기본값인 `"index"`가 적용되어, 같은 `_id`가 이미 있으면 덮어씁니다
  (upsert-by-replace).
- `op_type="index"`: 위와 같은 의미를 명시적으로 표현합니다.
- `op_type="create"`: 같은 `_id`가 이미 있으면 409 conflict로 실패합니다.
  duplicate가 곧 bug인 append-only ingestion에 적합합니다.

```python
doc_service.bulk_index_dataframe(
    df,
    id_field="doc_id",
    op_type="create",
)
```

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

### 전체 예제: CSV에서 OpenSearch로 DataFrame bulk insert

end-to-end workflow 예제입니다. CSV를 읽어 DataFrame으로 만들고, 최소한의
cleanup을 거친 뒤 `bulk_index_dataframe()`로 밀어넣습니다.

```python
import pandas as pd

from ops_store import OSConfig, OSDoc

df = pd.read_csv("articles.csv")

# 1) id column은 unique해야 합니다. 중복을 미리 걸러내는 쪽이 안전합니다.
df = df.drop_duplicates(subset="doc_id")

# 2) NaN/NaT/object column을 bulk action으로 보낼 수 있는 형태로 정리합니다.
#    (NaN, NaT, numpy scalar, Decimal 등은 normalize_document가 자동 처리하지만
#     datetime은 tz-aware로 맞춰 두는 편이 ingestion 결과를 예측하기 쉽습니다.)
df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
df["title"] = df["title"].fillna("")

# 3) service 생성. 환경 변수(OPENSEARCH_HOST 등)를 쓰고 있다면 config 인자 없이
#    `OSDoc(index="articles")`만으로도 충분합니다.
config = OSConfig(
    host="search.example.internal",
    user="admin",
    password="admin",
)
doc_service = OSDoc(config=config, index="articles")

# 4) bulk insert. default op_type은 "index"이므로 같은 doc_id가 있으면 덮어씁니다.
success, errors = doc_service.bulk_index_dataframe(
    df,
    id_field="doc_id",
    chunk_size=1000,
    refresh=True,
)

print(f"indexed {success} rows, {len(errors)} errors")
```

`bulk_index_dataframe()`는 `(success_count, errors)` tuple을 돌려줍니다.
`raise_on_error=False` (default) 상태에서는 실패한 action이 `errors` list에
들어오므로, ingestion 이후 로그로 남기거나 재시도 logic으로 이어가면 됩니다.

#### append-only ingestion: `op_type="create"`

동일 `_id`가 이미 있으면 **덮어쓰는 게 아니라 에러로 만들고 싶을 때**는
`op_type="create"`를 함께 넘깁니다.

```python
success, errors = doc_service.bulk_index_dataframe(
    df,
    id_field="doc_id",
    op_type="create",
    refresh=True,
)

# errors 각각은 {"create": {"_id": ..., "status": 409, "error": {...}}} 형태
duplicate_ids = [
    item["create"]["_id"]
    for item in errors
    if item.get("create", {}).get("status") == 409
]
```

#### `id_field` 없이 auto-generated `_id` 사용

`id_field`를 지정하지 않으면 OpenSearch가 `_id`를 생성합니다. event log나
audit trail처럼 document identity가 중요하지 않은 경우에 유용합니다.

```python
doc_service.bulk_index_dataframe(df, refresh=True)
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

### 예제: 특정 field에서 keyword 검색하기

가장 흔한 패턴은 특정 field에 대해 `match` query를 보내는 것입니다.
`OSSearch.match(field, query)`가 바로 그 용도입니다.

아래 예제는 `title` field에서 `"flask"`라는 keyword를 검색하고, 결과의
`hits`를 순회하는 가장 기본적인 흐름입니다.

```python
from ops_store import OSDoc, OSIndex, OSSearch

index_service = OSIndex(index="articles")
doc_service = OSDoc(index="articles")
search_service = OSSearch(index="articles")

if not index_service.exists():
    index_service.create(
        mappings={
            "properties": {
                "title": {
                    "type": "text",
                    "fields": {
                        "keyword": {"type": "keyword"},
                    },
                },
                "status": {"type": "keyword"},
                "body": {"type": "text"},
            }
        }
    )

doc_service.index(
    {
        "title": "Flask OpenSearch Guide",
        "status": "published",
        "body": "How to search documents with ops_store.",
    },
    doc_id="post-1",
    refresh=True,
)
doc_service.index(
    {
        "title": "FastAPI Tips",
        "status": "draft",
        "body": "This document should not match the flask title query.",
    },
    doc_id="post-2",
    refresh=True,
)

result = search_service.match("title", "flask", size=10)

for hit in result["hits"]["hits"]:
    source = hit["_source"]
    print(hit["_id"], hit["_score"], source["title"], source["status"])
```

이 예제에서 중요한 점:

- `match("title", "flask")`는 `{"query": {"match": {"title": "flask"}}}`를
  만들어서 보냅니다.
- `match()`는 보통 `text` field 검색에 적합합니다.
- 반환값은 OpenSearch raw response이므로 결과 document는
  `result["hits"]["hits"]`에서 직접 꺼내면 됩니다.

### exact match가 필요하면 `term()` 사용

`match()`는 analyzer를 거치는 full-text query입니다. exact value 비교가
필요하면 `term()`을 사용해야 합니다.

예를 들어 `status`가 `keyword` mapping이라면 아래처럼 조회합니다.

```python
published = search_service.term("status", "published", size=10)
```

만약 exact title 비교가 필요하고 mapping에 `title.keyword` subfield가 있다면
다음처럼 검색할 수 있습니다.

```python
exact_title = search_service.term(
    "title.keyword",
    "Flask OpenSearch Guide",
    size=1,
)
```

### 여러 field를 동시에 filter — `filter_terms()`

`term()`은 한 field, 한 value 기준입니다. 실제 filter UI나 API에서는
"field별로 허용 값 리스트"를 여러 개 동시에 걸어야 할 때가 많습니다.

- field별 value list는 OR (`[a, b, c]` → 해당 field가 `a`, `b`, `c` 중 하나면 매치)
- field끼리는 AND (field A 조건 **그리고** field B 조건)

이 패턴을 한 번에 표현해 주는 것이 `filter_terms()`입니다. 내부적으로
`bool.filter` context에 `terms` clause를 field마다 하나씩 붙이는 구조라,
점수 계산이 없고 shard-level cache도 활용됩니다.

```python
from ops_store import OSSearch

search_service = OSSearch(index="articles")

result = search_service.filter_terms(
    {
        "category.keyword": ["books", "movies"],
        "status": ["published", "featured"],
    },
    size=50,
)
```

위 호출은 아래 query body를 생성합니다.

```json
{
  "query": {
    "bool": {
      "filter": [
        {"terms": {"category.keyword": ["books", "movies"]}},
        {"terms": {"status": ["published", "featured"]}}
      ]
    }
  }
}
```

#### exact match 동작과 `.keyword` subfield

`filter_terms()`는 내부적으로 `terms` query를 사용하므로 analyzer를 거치지
않는 **exact** 비교입니다. 대소문자, 공백, 구두점이 모두 그대로 비교되므로
text field에서 exact match가 필요하면 `title.keyword`처럼 `.keyword`
subfield를 caller가 직접 지정해야 합니다. 자동으로 붙이지는 않습니다.

- `keyword` type field → field 이름 그대로 사용 (`status`, `category` 등)
- `text` type field에서 exact match → `.keyword` subfield 사용 (`title.keyword`)

#### `minimum_should_match`로 field AND를 완화

기본 동작은 모든 field가 매치해야 합니다 (AND across fields). "지정한 K개
field 중 적어도 N개가 매치하면 된다"로 완화하고 싶다면
`minimum_should_match`를 넘깁니다. 이 값은 **field 단위**로 적용됩니다.

```python
# 세 개 field 중 적어도 두 개가 매치하면 포함
search_service.filter_terms(
    {
        "category.keyword": ["books"],
        "status": ["published"],
        "tag": ["hot"],
    },
    minimum_should_match=2,
)
```

`minimum_should_match=None` (default): 모든 field가 매치해야 함.
`minimum_should_match=N`: K개 중 N개 이상 매치하면 포함.

주의: 여기서의 `minimum_should_match`는 **각 field 안의 value 개수**가
아니라 **field 자체의 개수**에 대한 임계값입니다. "한 field 안에서 value
리스트 중 몇 개 이상 토큰이 포함돼야 한다"는 semantics는 지원하지
않습니다. 그 경우에는 `bool()` helper로 직접 clause를 구성해야 합니다.

#### 빈 value list는 무시

특정 field의 value가 빈 list면 해당 field는 query에서 조용히 빠집니다.
UI에서 아직 선택되지 않은 filter 항목을 그대로 전달해도 zero-hit
함정에 빠지지 않게 하기 위한 동작입니다.

```python
search_service.filter_terms(
    {
        "category.keyword": ["books"],
        "status": [],          # 전달되긴 했지만 query에 포함되지 않음
    }
)
```

#### 추가 scoring query와 결합

filter는 점수를 계산하지 않습니다. filter 결과 안에서 별도의 match
query로 정렬하고 싶다면 `query=`에 일반 query clause를 넘깁니다. 이
clause는 `bool.must`로 들어가므로 점수에 반영됩니다.

```python
search_service.filter_terms(
    {"status": ["published"]},
    query={"match": {"title": "flask"}},
    size=20,
)
```

이 호출은 `status="published"`인 문서 집합 안에서 `title`에 `flask`가
매치되는 정도로 순위를 매깁니다.

#### 언제 `filter_terms()`가 적합하고 언제 적합하지 않은가

적합한 상황:

- status / category / tag 같은 dropdown filter 구성
- ID, UUID, enum 값 매칭
- "이 값들 중 하나" 조건이 여러 field에 걸쳐 필요한 경우

적합하지 않은 상황:

- 자유 텍스트 검색 → `match()` / `multi_match()` 사용
- case-insensitive 매칭이 필요 → mapping에 `lowercase` normalizer를 붙이거나
  `match()`를 사용
- 부분/접두 매칭 → `prefix`, `wildcard`, edge-ngram analyzer 사용

#### `index=` 인자는 언제 넘기나

다른 search helper와 동일한 규칙입니다. 대부분은 service 생성 시
`OSSearch(index="articles")`처럼 default index를 지정해 두고, 호출 시에는
따로 넘기지 않습니다. 특정 backing index로 강제로 보내야 할 때(예:
`articles-000002`에만 질의) 또는 여러 index를 wildcard로 한 번에 타겟할
때만 call site에 `index=`를 추가합니다.

```python
# 기본 경로: service default index(보통 alias) 사용
search_service.filter_terms({"status": ["published"]})

# 특정 backing index로 override
search_service.filter_terms(
    {"status": ["published"]},
    index="articles-000002",
)
```

### 바로 `pandas.DataFrame`으로 받고 싶을 때

검색 결과를 바로 분석 코드로 넘기고 싶다면 `match_dataframe()`을 사용할 수
있습니다. 이 helper는 내부적으로 `match()`를 호출한 뒤 `hits["hits"]`를
`DataFrame`으로 바꿔줍니다.

```python
from ops_store import OSSearch

search_service = OSSearch(index="articles")

df = search_service.match_dataframe("title", "flask", size=100)
print(df[["title", "status"]].head())
```

기본값에서는 `_source` field만 column으로 들어갑니다. `_id`, `_index`,
`_score`도 함께 보고 싶다면 `include_meta=True`를 사용합니다.

```python
df = search_service.match_dataframe(
    "title",
    "flask",
    size=100,
    include_meta=True,
)

print(df[["_id", "_score", "title"]].head())
```

보다 복잡한 raw query body를 직접 쓰고 싶다면 `search_dataframe()`을 사용하면
됩니다.

```python
df = search_service.search_dataframe(
    {
        "query": {
            "bool": {
                "must": [{"match": {"title": "flask"}}],
                "filter": [{"term": {"status": "published"}}],
            }
        },
        "size": 100,
    },
    include_meta=True,
)
```

matching document를 전부 가져오고 싶다면 `size=10000`만 올리는 방식보다
scroll 기반 helper를 사용하는 편이 안전합니다. 이를 위해
`match_dataframe_all()`과 `search_dataframe_all()`이 추가되어 있습니다.

```python
df = search_service.match_dataframe_all(
    "title",
    "flask",
    batch_size=1000,
    include_meta=True,
)
```

raw query body를 그대로 쓰는 경우:

```python
df = search_service.search_dataframe_all(
    {
        "query": {
            "bool": {
                "must": [{"match": {"title": "flask"}}],
                "filter": [{"term": {"status": "published"}}],
            }
        }
    },
    batch_size=1000,
    scroll="2m",
    include_meta=True,
)
```

중요한 점:

- 이 기능은 optional dependency인 `pandas`가 설치되어 있어야 합니다.
- `match()`와 `search_raw()`는 기존처럼 raw OpenSearch response를 그대로
  반환합니다.
- `match_dataframe()`과 `search_dataframe()`은 한 번의 search response만
  `DataFrame`으로 바꿉니다.
- 전체 결과 export가 필요하면 `match_dataframe_all()` 또는
  `search_dataframe_all()`을 사용하세요.
- `batch_size`는 scroll 한 번에 가져오는 문서 수입니다. 전체 결과 수와는
  다릅니다.
- exact match 결과를 `DataFrame`으로 받고 싶다면
  `search_service.to_dataframe(search_service.term("status", "published"))`
  처럼 조합해서 사용할 수 있습니다.

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

### 시간 기준으로 가장 최근 document 가져오기 — `latest()`

특정 time field 기준으로 최신 document를 뽑을 때 `OSSearch.latest()`를
사용합니다. 내부적으로 `sort: [{<time_field>: {"order": "desc"}}]`를 붙여서
`search`를 호출합니다.

time field 이름은 index마다 다를 수 있으므로 인자로 직접 넘겨야 합니다
(`event_tm`, `created_at`, `logged_at` 등).

```python
from ops_store import OSSearch

events = OSSearch(index="events")

# 가장 최근 1건
latest = events.latest("event_tm")
newest_doc = latest["hits"]["hits"][0]["_source"]

# 최근 10건
recent = events.latest("event_tm", size=10)

# 특정 조건 안에서 가장 최근 doc (예: user_id="u-1"의 최신 이벤트)
user_latest = events.latest(
    "event_tm",
    query={"term": {"user_id": "u-1"}},
)

# 호출 시점에 index를 override
override = events.latest("event_tm", index="events-000002")
```

중요한 점:

- `latest()`는 search를 보내기 전에 해당 index의 mapping을 조회해서 `time_field`가
  `date` 또는 `date_nanos` 타입인지 검증합니다. `keyword`/`text` 같은 다른 타입이면
  `ValueError`를 던집니다 — 문자열 비교로 정렬되어 "최신 doc"이 틀리게 나오는
  사고를 방지하기 위해서입니다.
- alias 뒤에 backing index가 여러 개 붙어 있는 경우(rollover 구성), `latest()`는
  모든 backing index의 mapping을 확인합니다. 그 중 하나라도 time field가
  date 계열이 아니면 에러가 납니다.
- mapping에 해당 field가 없으면 역시 `ValueError`가 납니다.
- nested field는 dotted path로 넘기면 됩니다: `events.latest("event.tm")`.
- 반환값은 raw OpenSearch response이므로, DataFrame으로 받고 싶다면
  `events.to_dataframe(events.latest("event_tm", size=100))`처럼 조합합니다.

### index에 어떤 data가 있는지 빠르게 훑어보기 — `sample()`

`sample()`은 `function_score` + `random_score`로 무작위 N건을 가져옵니다.
schema를 파악하거나 EDA용으로 빠르게 데이터를 훑을 때 유용합니다.

```python
from ops_store import OSSearch

events = OSSearch(index="events")

# 전체 중 무작위 10건 (default)
rows = events.sample()
for hit in rows["hits"]["hits"]:
    print(hit["_id"], hit["_source"])

# 개수 조정
thirty = events.sample(size=30)

# 특정 조건 안에서 무작위 샘플링
ok_sample = events.sample(
    size=20,
    query={"term": {"status": "ok"}},
)

# 재현 가능한 샘플링 (같은 seed ⇒ 같은 결과)
reproducible = events.sample(size=10, seed=42)
```

중요한 점:

- `seed`를 주지 않으면 호출마다 결과가 달라집니다. 같은 seed로 항상 같은
  샘플을 받고 싶다면 `seed=<int>`를 넘기세요. OpenSearch는 seed 사용 시
  segment-level 일관성을 위해 필드를 요구하므로 `sample()`은 자동으로
  `_seq_no`를 사용합니다.
- `query`는 `function_score`의 inner query로 들어갑니다. 즉 "조건을 만족하는
  document 중에서 무작위로 N건" 이라는 의미가 됩니다.
- 반환값은 raw response이므로 `to_dataframe()`과 조합하면 DataFrame으로
  바로 받을 수 있습니다: `events.to_dataframe(events.sample(size=100))`.

### 특정 field의 unique value 목록이 필요할 때 — `unique_values()`

category/status 같은 field에 어떤 값이 들어 있는지 한 번에 보고 싶을 때
사용합니다. 내부적으로 `terms` aggregation을 돌린 뒤 bucket key들만 꺼내서
list로 돌려줍니다.

```python
from ops_store import OSSearch

articles = OSSearch(index="articles")

# text field라면 keyword sub-field를 지정해야 합니다.
statuses = articles.unique_values("status")
categories = articles.unique_values("category.keyword", size=500)

# 조건을 건 후 그 안에서만 unique value를 보고 싶다면 query를 넘깁니다.
published_categories = articles.unique_values(
    "category.keyword",
    query={"term": {"status": "published"}},
)
```

중요한 점:

- `terms` aggregation은 기본적으로 `keyword`(혹은 numeric/boolean) 타입 field에서
  동작합니다. `text` field에 대해서는 `fielddata`를 켜지 않는 한 실패하므로,
  보통은 `title.keyword` 처럼 sub-field를 지정하게 됩니다.
- `size`는 반환할 bucket 개수 상한입니다. cardinality가 매우 큰 field에서
  전부 필요하면 composite aggregation 같은 다른 접근이 필요합니다.

## Logging 동작

`ops_store`는 OpenSearch 호출을 자체 로깅하지 않습니다. 클러스터 상태는
OpenSearch/Kibana 대시보드 또는 별도 monitoring 서비스로 관찰하는 것을
전제로 합니다. Flask application 로그가 필요하면 repository root의
`logging_config.py`를 사용하세요.

OpenSearch 자체를 로그 저장소로 쓰는 전반적인 전략은
`ops_store/docs/logging_strategy.md`를 참고하세요.

## 올바른 사용 규칙

이 패키지를 안정적으로 쓰기 위해 가장 중요한 규칙은 아래와 같습니다.

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
from ops_store import OSDoc

doc_service = OSDoc(index="articles")


def get_article(article_id: str):
    result = doc_service.get(article_id)
    return jsonify(result)
```

이렇게 하면 Flask는 HTTP를 담당하고, `ops_store`는 OpenSearch 관련 동작을
담당하게 되어 역할이 깔끔하게 나뉩니다.

# 코드베이스 가이드

## 이 repository가 담고 있는 것

이 repository는 크게 두 부분으로 나뉩니다.

1. HTTP entrypoint 역할을 하는 아주 작은 Flask app
2. `opensearch-py`를 얇게 감싼 재사용 가능한 `ops_store` 패키지

Flask app은 의도적으로 얇게 유지되어 있고, 재사용 가능한 핵심 로직은
대부분 `ops_store/` 안에 있습니다.

## Repository 구조

- `index.py`: 최상위 process entrypoint. Flask app을 만들고 로컬에서 실행합니다.
- `config.py`: environment variable 기반 Flask 설정
- `api/__init__.py`: application factory와 루트 `/` route 등록
- `api/routes.py`: `/api` 아래의 blueprint route
- `ops_store/base.py`: connection config, client factory, 공통 base class
- `ops_store/document.py`: document CRUD와 bulk write helper
- `ops_store/index.py`: index 설정, mapping, refresh, alias, rollover helper
- `ops_store/search.py`: raw, lexical, boolean, vector, aggregation query helper
- `tests/test_ops_store_services.py`: config, CRUD, query 구성에 대한
  unit test

## Runtime 흐름

요청과 service 흐름은 단순합니다.

1. `index.py`가 `api`에서 `create_app()`을 import 합니다.
2. `api.create_app()`이 Flask app을 만들고 `Config`를 로드한 뒤 `/api`
   blueprint를 등록합니다.
3. `api/routes.py`의 route function이 JSON response를 반환합니다.
4. application code에서 OpenSearch 접근이 필요하면 `ops_store`를 사용합니다.
5. `ops_store.base.load_config()`가 environment variable을 읽어 `OSConfig`를
   만듭니다.
6. `ops_store.base.create_client()`가 `OSConfig`를 실제 `OpenSearch` client로
   바꿉니다.
7. `OSDoc`, `OSIndex`, `OSSearch`는 모두 `OSBase`를 상속하고, 공통 client
   lifecycle 규칙을 공유합니다. OpenSearch 호출 결과는 raw response 그대로
   호출자에게 반환됩니다.

## `ops_store` module별 역할

### `base.py`

`base.py`는 패키지의 기반입니다.

- `OSConfig`: OpenSearch connection 설정을 담는 dataclass
- `OSConfig.from_env()`: 지원되는 모든 `OPENSEARCH_*` environment variable
  로드
- `create_client()`: 실제 `opensearchpy.OpenSearch` client 생성
- `OSBase`: client, optional config, optional default index를 보관하는 공통
  base class

이 module에서 중요한 동작:

- `user`를 설정하면 `password`도 반드시 설정해야 하고, 반대도 동일합니다.
- `use_ssl=True`이면 host scheme이 `https`로 바뀝니다.
- 기본 connection profile은 HTTPS `443` port, `verify_certs=False`,
  `ssl_show_warn=False`입니다.
- `OSBase._resolve_index()`는 명시적인 `index` argument와
  `default_index`가 모두 없으면 `ValueError`를 발생시킵니다.
- 이미 생성된 `client`를 넘긴 경우에는 client override keyword argument를
  함께 넘길 수 없습니다. 모호한 설정 조합을 막기 위한 제약입니다.

### `document.py`

`OSDoc`은 document 단위 read/write를 담당합니다.

- `index()`: 단일 document 생성 또는 교체
- `get()`: id로 document 조회
- `update()`: `{"doc": ...}` 형태의 partial update
- `upsert()`: `doc_as_upsert=True`를 사용하는 update-or-insert
- `delete()`: id 기준 document 삭제
- `bulk()`: raw bulk action 전송
- `bulk_index()`: 일반 document dictionary를 bulk index action으로 변환
- `bulk_index_dataframe()`: DataFrame row를 stream 형태로 bulk index action으로
  변환
- `normalize_document()`: pandas/NumPy 친화적인 값을 JSON-safe document로 변환

`bulk_index()`는 `bulk()`보다 한 단계 높은 수준의 helper입니다.
document mapping sequence를 입력으로 받고, `id_field`를 지정하면 해당 field를
`_id`로 사용합니다.

pandas 중심 workflow에서는 `bulk_index_dataframe()`이 더 안전하고 메모리
효율적입니다. missing value, timestamp, NumPy scalar 같은 JSON 비호환 값을
정규화한 뒤 OpenSearch bulk serializer로 넘기기 때문입니다.

### `index.py`

`OSIndex`는 `client.indices`를 감싼 index 관리 helper입니다.

- `exists()`: 기본적으로 concrete index와 alias를 모두 포함한 존재 여부 확인
- `alias_exists()`: alias namespace만 따로 확인
- `describe()`: 현재 이름이 index인지 alias인지, backing index/alias/rollover 구성을 요약
- `create()`: optional mapping, settings, aliases와 함께 index 생성
- `recreate_index()`: 기존 index가 있으면 삭제 후 shard/replica, mapping, alias를 포함해 다시 생성
- `rollover()`: alias를 다음 backing index로 rollover
- `delete()`: index 삭제
- `get_settings()`: 현재 index settings 조회
- `get_mapping()`: mapping 조회
- `update_settings()`: index settings 변경
- `refresh()`: index refresh
- `get_aliases()`: 특정 index의 alias 또는 전체 alias 조회
- `update_aliases()`: alias add/remove action 제출

`create()`는 별도 override가 없으면 다음 기본값을 적용합니다.

- `number_of_shards=1`
- `number_of_replicas=0`
- `refresh_interval="30s"`

data가 계속 쌓이는 time-series 또는 append-heavy workload에서는
alias + backing index 패턴을 의도한 사용 방식으로 보면 됩니다.

- app이 읽고 쓰는 stable alias 예: `articles`
- 실제 물리 index 예: `articles-000001`
- rollover는 concrete backing index가 아니라 alias 기준으로 수행

이렇게 하면 실제 index 개수가 늘어나도 application code는 거의 바뀌지
않습니다.

`exists()`가 alias도 `True`로 돌려주기 때문에 application startup에서 alias를
default index처럼 다뤄도 됩니다. 반대로 물리 index가 있는지만 보고 싶다면
`include_aliases=False`를 사용해야 합니다. `recreate_index()`는 내부적으로 이
concrete-index-only 동작을 사용하므로 alias만 있는 이름을 잘못 삭제하지
않습니다.

### `search.py`

`OSSearch`는 query helper를 제공하고, 기본값에서는 response를 그대로
OpenSearch 형식으로 반환합니다. 필요할 때만 `DataFrame` helper를 추가로
사용할 수 있게 설계되어 있습니다.

- `search_raw()`: raw search body 직접 전송
- `search_dataframe()`: raw search body 결과를 `DataFrame`으로 변환
- `search_dataframe_all()`: scroll로 전체 검색 결과를 `DataFrame`으로 변환
- `count()`: matching document 개수 조회
- `match()`: 단일 field match query
- `match_dataframe()`: 단일 field match query 결과를 `DataFrame`으로 변환
- `match_dataframe_all()`: 단일 field match 전체 결과를 `DataFrame`으로 변환
- `term()`: exact-value term query
- `bool()`: boolean query builder
- `multi_match()`: multi-field full-text query
- `knn()`: vector k-NN query
- `hybrid()`: lexical + vector `should` query
- `aggregate()`: aggregation search
- `to_dataframe()`: raw search result의 `hits["hits"]`를 `DataFrame`으로 변환

패키지는 자체 response model을 만들지 않습니다. `hits`, `aggregations`,
metadata를 어떻게 추출할지는 caller가 결정하도록 기본적으로 OpenSearch
response 형식을 그대로 유지합니다. `pandas`가 필요한 workflow에서는
`to_dataframe()`, `match_dataframe()`, `search_dataframe_all()` 같은 helper를
붙여서 바로 tabular data로 전환할 수 있습니다.

### Logging 정책

`ops_store`는 자체적으로 OpenSearch 호출을 로깅하지 않습니다. 클러스터
상태는 OpenSearch/Kibana 대시보드 또는 별도 monitoring 서비스로 관찰하고,
Flask application 로그가 필요하면 repository root의 `logging_config.py`를
사용하세요.

## 테스트가 검증하는 내용

`tests/test_ops_store_services.py`는 다음 핵심 동작을 검증합니다.

- environment 기반 config 로딩
- client 생성 argument 구성
- bulk action 구성
- query body 생성
- index 생성 시 기본 settings 적용

테스트는 live OpenSearch cluster 대신 `unittest.mock`을 사용합니다. 이
패키지가 실제 cluster와의 통신보다는 argument shaping과 delegation이 중심인
구조이기 때문에, 이런 unit test 방식이 잘 맞습니다.

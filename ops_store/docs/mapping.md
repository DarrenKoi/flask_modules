# mapping 이해하기

이 문서는 OpenSearch **mapping**(= index의 schema)의 구성 요소와
parameter를 예제 중심으로 정리합니다. `ops_store/index.py`의
`OSIndex.create`, `OSIndex.recreate_index`, `OSIndex.create_rollover_index`,
`OSIndex.rollover`가 공통으로 받는 `mappings` 인자는 여기서 설명하는 형식을
그대로 body에 실어 OpenSearch로 전달합니다.

`ops_store`는 mapping을 추상화하지 않습니다. 호출자가 만든 dict가 곧 cluster로
나가는 body입니다. 그래서 이 문서는 "helper가 뭘 해 주는가"가 아니라 "어떤
dict를 만들어야 하는가"에 집중합니다.

## 1. 개념 한 줄 정리

- **mapping**은 하나의 index(또는 template 매칭 대상)의 schema다.
- **field type**은 어떤 자료구조로 색인할지 결정한다. `keyword`, `text`,
  `long`, `date`, `object`, `nested`, `geo_point`, `ip`, `binary`, …
- **mapping parameter**는 field type 안에서 "어떻게 색인할지"를 조정한다.
  `index`, `doc_values`, `analyzer`, `enabled`, `copy_to`, `null_value`, …
- **dynamic mapping**은 새 field가 들어왔을 때 자동으로 mapping을 만들지,
  거부할지, 무시할지를 지정한다.
- mapping은 대부분 **immutable**이다. 이미 매핑된 field의 type을 바꿀 수
  없고, `index`/`analyzer` 같은 parameter도 바꿀 수 없다. 바꾸려면 새
  index를 만들어 reindex 해야 한다.

## 2. dynamic vs explicit mapping

아무 mapping 없이 document를 넣으면 OpenSearch는 JSON의 type을 보고
mapping을 **자동으로** 만든다(dynamic mapping). 편하지만 두 가지 위험이
있다.

- JSON string은 기본적으로 `text` + `keyword` multi-field로 매핑된다.
  진짜 원한 건 `keyword` 하나였는데 inverted index + fielddata까지 세팅되어
  cluster 메모리를 먹는다.
- 우연히 들어간 오타 field(`timstamp` 같은)도 mapping에 추가되어 버린다.
  mapping은 immutable이라 이 쓰레기가 남는다.

그래서 production에서는 **explicit mapping + `dynamic: strict`** 조합을 권장한다.

```python
from ops_store import OSIndex

svc = OSIndex(index="articles")
svc.create(
    mappings={
        "dynamic": "strict",   # 선언 안 한 field가 들어오면 reject
        "properties": {
            "article_id": {"type": "keyword"},
            "title":      {"type": "text"},
            "created_at": {"type": "date"},
        },
    },
)
```

`dynamic` value는 네 가지다.

| value      | 동작 |
|------------|------|
| `true`     | 새 field를 자동으로 mapping에 추가(기본값) |
| `runtime`  | runtime field로 등록(`_source`에 남지만 색인은 지연) |
| `false`    | `_source`에는 저장, mapping에는 추가하지 않음 → 검색/집계 불가 |
| `strict`   | 선언 안 한 field가 있으면 document reject |

## 3. 자주 쓰는 field type

| type        | 언제 쓰나 | 특이 parameter |
|-------------|-----------|----------------|
| `keyword`   | 정확히 일치 검색, 정렬, 집계. ID, enum, tag, URL | `normalizer`, `ignore_above` |
| `text`      | full-text 검색(analyzer 통과) | `analyzer`, `search_analyzer`, `fielddata` |
| `long`/`integer`/`short`/`byte` | 정수 | `coerce`, `null_value` |
| `double`/`float`/`half_float` | 실수 | 동일 |
| `scaled_float` | 고정 소수점(가격 등) | `scaling_factor` 필수 |
| `boolean`   | 참/거짓 | `null_value` |
| `date`      | 날짜/시각 | `format`(여러 format pipe로 연결 가능) |
| `ip`        | IPv4/IPv6 | `null_value` |
| `binary`    | base64 blob (기본 `index: false`) | `doc_values` |
| `object`    | 중첩 dict (평탄화되어 색인) | `enabled`, `dynamic`, `properties` |
| `nested`    | 독립적으로 쿼리해야 하는 dict array | `properties`, `include_in_parent`, `include_in_root` |
| `geo_point` | 좌표 | 없음 |
| `geo_shape` | 도형 | `strategy`, `tree` |
| `join`      | parent/child 관계 | `relations` |
| `flattened` | 내부 구조를 전부 keyword로 납작하게 | 전용 type |
| `search_as_you_type` | prefix autocomplete | `max_shingle_size` |
| `dense_vector`(OS 2.x에서는 `knn_vector`) | 벡터 검색 | `dimension`, `method` |

### keyword vs text — 가장 흔한 선택

```jsonc
{
  "title": {
    "type": "text",                    // 토큰화해서 full-text 검색
    "fields": {
      "raw": {"type": "keyword"}       // 정확히 일치 / 정렬용 sub-field
    }
  }
}
```

- `title`만 놓고 `match` query → 분석기 통과, "빅 데이터"도 "데이터 빅"으로
  매치됨.
- `title.raw`는 `term` query, `sort`, `aggs`에 적합.

`text` 하나만 두고 `sort`나 `aggs`를 하려 하면 기본적으로 거부된다.
정말 필요하면 `fielddata: true`를 켜야 하는데 heap을 많이 쓴다. 대신
`.raw` sub-field를 두는 게 관용적인 패턴이다.

### object vs nested — 가장 흔한 함정

`object`는 내부 dict를 **평탄화**해 color:red / color:blue 같은 array가
들어오면 "어느 문서에 red가 있음 + 어느 문서에 blue가 있음"으로 흩어진다.
그래서 "같은 item의 color가 red이고 size가 M인 것"을 찾을 수 없다.

```jsonc
// doc
{"items": [{"color": "red", "size": "L"}, {"color": "blue", "size": "M"}]}

// object로 매핑되면 내부적으로 이렇게 보관된다
// items.color = ["red", "blue"]
// items.size  = ["L", "M"]
// → 아래 query가 (red + M) 조합에도 매치되어 버린다
{"bool": {"must": [{"term": {"items.color": "red"}},
                   {"term": {"items.size":  "M"}}]}}
```

독립 쿼리가 필요하면 `nested`로 매핑하고 `nested` query를 쓴다.

```jsonc
{
  "items": {
    "type": "nested",
    "properties": {
      "color": {"type": "keyword"},
      "size":  {"type": "keyword"}
    }
  }
}
```

`nested`는 각 element를 내부적으로 숨겨진 별도 document로 색인한다. 검색
비용이 object보다 크지만 정확한 질의가 가능하다.

## 4. 중요한 mapping parameter

### `index` — inverted index를 만들 것인가

```jsonc
{"trace_id": {"type": "keyword", "index": false}}
```

- `false`면 inverted index를 만들지 않음 → `term`, `match` 질의로 검색 불가.
- 그래도 `doc_values`는 기본 true → `sort`, `aggs`는 가능.
- 그래도 `_source`에 들어 있으므로 `GET /_doc/<id>`로 받아볼 수 있음.
- store-only column이 필요할 때 쓴다.

### `doc_values` — column store를 만들 것인가

```jsonc
{"massive_tag": {"type": "keyword", "doc_values": false}}
```

- 기본 true. `sort`, `aggs`, `script`에 쓰이는 컬럼 저장소.
- true면 disk를 더 쓴다. 정렬/집계 대상이 절대 아닌 field라면 false로
  줄일 수 있다.

### `enabled` — object 전체 색인을 끌 것인가

```jsonc
{"raw_payload": {"type": "object", "enabled": false}}
```

- **object / 루트 mapping에만 유효하다.** `keyword`, `date`, `long` 같은
  leaf type에는 못 쓴다.
- `enabled: false`면 이 subtree는 **parse조차 안 된다.** `_source`에는
  그대로 남아 `GET /_doc/<id>`로 돌려주지만, 내부 field를 검색/집계/sort
  할 수 없고 `exists` query도 못 한다.
- mapping explosion을 막거나, 스키마가 일정하지 않은 third-party payload를
  통째로 저장할 때 쓴다.

### `enabled: false` vs `index: false` vs `keyword` — 헷갈리는 3형제

| 목표 | mapping | 검색? | sort/aggs? | `_source`에 남음? |
|------|---------|-------|------------|-------------------|
| object subtree 전체 parse를 끊음 | `{"type":"object","enabled":false}` | ❌ | ❌ | ✅ |
| scalar는 저장하되 inverted index 생략 | `{"type":"keyword","index":false}` | ❌ | ✅ (doc_values) | ✅ |
| 문자열을 토큰화 없이 정확히 일치 | `{"type":"keyword"}` | ✅ exact | ✅ | ✅ |
| 문자열을 토큰화해서 full-text | `{"type":"text"}` | ✅ | ❌ (fielddata 필요) | ✅ |

### `analyzer` / `search_analyzer`

`text` field의 토큰화 규칙을 지정한다. `search_analyzer`를 따로 주지 않으면
색인과 질의 모두 같은 analyzer를 쓴다. 한국어에 `nori`를 주로 쓰고, 영문
그대로는 `standard`(기본)나 `english`가 흔하다.

```jsonc
{
  "body": {
    "type": "text",
    "analyzer": "nori",
    "search_analyzer": "nori"
  }
}
```

### `normalizer` (keyword 전용)

토큰화는 하지 않되 lowercasing / asciifolding 같은 character filter만
적용한다. email, tag처럼 대소문자 무시하고 exact match가 필요한 경우.

```jsonc
{
  "email": {"type": "keyword", "normalizer": "lowercase"}
}
```

### `copy_to`

여러 field 값을 하나의 virtual field로 모아 전체 검색(all-in-one) 용도로
쓴다.

```jsonc
{
  "title":   {"type": "text", "copy_to": "search_all"},
  "summary": {"type": "text", "copy_to": "search_all"},
  "search_all": {"type": "text"}
}
```

`search_all`은 `_source`에 들어가지는 않고 색인에만 존재한다.

### `null_value`

JSON `null`은 기본적으로 색인되지 않는다. 명시적으로 "null이면 이 값으로
친다"를 지정하려면:

```jsonc
{"status": {"type": "keyword", "null_value": "UNKNOWN"}}
```

### `ignore_above` (keyword 전용)

긴 문자열로 인한 mapping 폭주/메모리 문제를 방지.

```jsonc
{"url": {"type": "keyword", "ignore_above": 2048}}
```

2048자를 넘는 값은 **색인되지 않는다.** `_source`에는 남는다.

### `format` (date 전용)

```jsonc
{"event_time": {
  "type": "date",
  "format": "yyyy-MM-dd HH:mm:ss||strict_date_optional_time||epoch_millis"
}}
```

`||`로 여러 포맷을 OR로 나열할 수 있다. ingest 경로에 timezone이 있는
문자열과 epoch millis가 섞여 들어온다면 그걸 모두 받는 식으로 설계한다.

### `store`

보통 건드리지 않는 parameter. `_source`가 기본적으로 원본을 담고 있으므로
개별 field를 또 저장할 필요가 거의 없다. `_source`를 껐을 때(`"_source":
{"enabled": false}`)만 의미가 생긴다.

## 5. multi-field (`fields`)

하나의 source field를 **여러 방식으로** 색인해 둔다.

```jsonc
{
  "name": {
    "type": "text",
    "analyzer": "standard",
    "fields": {
      "raw":       {"type": "keyword"},
      "ngram":     {"type": "text", "analyzer": "autocomplete_ngram"},
      "lowercase": {"type": "keyword", "normalizer": "lowercase"}
    }
  }
}
```

- `name` → full-text match
- `name.raw` → 정확 일치, 정렬, 집계
- `name.ngram` → autocomplete
- `name.lowercase` → 대소문자 무시 exact match

Query에서는 `name.raw`처럼 dotted path로 지정한다. document에 따로
데이터를 보낼 필요는 없다(하나의 source → 여러 색인).

## 6. dynamic template

"새 field가 들어오면 이 pattern일 때는 이렇게 매핑하라"는 규칙.

```jsonc
{
  "dynamic_templates": [
    {
      "strings_as_keyword": {
        "match_mapping_type": "string",
        "mapping": {"type": "keyword", "ignore_above": 256}
      }
    },
    {
      "ip_fields": {
        "match": "*_ip",
        "mapping": {"type": "ip"}
      }
    }
  ],
  "properties": {
    "host": {"type": "keyword"}
  }
}
```

- `match_mapping_type`: 들어온 JSON type으로 매칭 (`string`, `long`, `boolean`, `object`, ...).
- `match` / `unmatch`: field 이름 패턴.
- `path_match` / `path_unmatch`: dotted path 패턴(`meta.*_ts`).
- 위에서 아래로 평가, 첫 매칭만 적용. **`properties`에 이미 선언된 field는
  template을 거치지 않는다.**

log 쪽에서 "문자열은 전부 keyword로", "*_at으로 끝나는 건 date로" 같은
rule을 걸어 두면 편하다.

## 7. 실제 예제 세 개

### 7.1 logs 스타일 index

```python
svc = OSIndex(index="ingest-logs-000001")
svc.create(
    mappings={
        "dynamic": "false",                # 선언 외 field는 _source에만 남김
        "date_detection": False,
        "properties": {
            "@timestamp": {"type": "date"},
            "level":      {"type": "keyword"},
            "service":    {"type": "keyword"},
            "message":    {"type": "text", "analyzer": "standard"},
            "host":       {"type": "keyword"},
            "user_id":    {"type": "keyword"},
            "trace_id":   {"type": "keyword", "index": False},
            "payload":    {"type": "object", "enabled": False},
        },
        "dynamic_templates": [
            {"strings_as_keyword": {
                "match_mapping_type": "string",
                "mapping": {"type": "keyword", "ignore_above": 1024}
            }}
        ],
    },
    settings={"refresh_interval": "30s"},
)
```

포인트:
- 로그는 지정 스키마 외에는 noise가 많으니 `dynamic: "false"`로 받아서
  `_source`에만 남긴다.
- `trace_id`는 search는 안 하고 grep용이라면 `index: false`.
- `payload`는 통째로 `enabled: false`로 두면 mapping 폭주를 막으면서도
  내용은 응답에 돌려 준다.

### 7.2 articles(검색 중심) index

```python
svc = OSIndex(index="articles")
svc.create(
    mappings={
        "dynamic": "strict",
        "properties": {
            "article_id":  {"type": "keyword"},
            "title": {
                "type": "text", "analyzer": "nori",
                "fields": {"raw": {"type": "keyword"}}
            },
            "body":        {"type": "text", "analyzer": "nori"},
            "tags":        {"type": "keyword"},
            "author": {
                "type": "object",
                "properties": {
                    "id":   {"type": "keyword"},
                    "name": {"type": "text",
                             "fields": {"raw": {"type": "keyword"}}}
                }
            },
            "created_at":  {"type": "date"},
            "updated_at":  {"type": "date"},
            "view_count":  {"type": "long"},
            "embedding": {
                "type": "knn_vector", "dimension": 768,
                "method": {"name": "hnsw", "space_type": "cosinesimil"}
            }
        }
    },
    settings={"refresh_interval": "1s", "knn": True},
)
```

포인트:
- 제목/본문 full-text + `.raw`로 정렬/집계.
- author는 **object**로 충분(배열이 아니므로 평탄화 문제가 없다).
- kNN vector까지 한 index에 두면 hybrid 검색에 편하다.

### 7.3 orders(nested 필요) index

```python
svc = OSIndex(index="orders")
svc.create(
    mappings={
        "dynamic": "strict",
        "properties": {
            "order_id":  {"type": "keyword"},
            "customer_id": {"type": "keyword"},
            "placed_at": {"type": "date"},
            "total_krw": {"type": "scaled_float", "scaling_factor": 100},
            "line_items": {
                "type": "nested",       # 같은 line item 조건을 유지해야 함
                "properties": {
                    "sku":      {"type": "keyword"},
                    "qty":      {"type": "integer"},
                    "price":    {"type": "scaled_float", "scaling_factor": 100},
                    "category": {"type": "keyword"}
                }
            }
        }
    },
)
```

포인트:
- "같은 주문 line에서 sku=X이고 qty>=2"를 묻고 싶다면 `nested`가 필수.
- 없이 `object`로 두면 sku와 qty가 cross-match되어 잘못된 결과가 나온다.

## 8. ops_store helper와의 연결

`ops_store/index.py`에서 `mappings` dict를 받는 진입점:

| helper | 역할 | mapping 전달 위치 |
|--------|------|--------------------|
| `OSIndex.create` | 새 index 생성 | `mappings=` (body.mappings) |
| `OSIndex.recreate_index` | 기존 index drop 후 재생성 | `mappings=` |
| `OSIndex.create_rollover_index` | `name-000001` + `name_alias` 생성 | `mappings=` |
| `OSIndex.rollover` | 조건부 rollover | `mappings=` (rollover body에 include) |
| `OSIndex.get_mapping` | 현재 mapping 조회 | — |

예제:

```python
from ops_store import OSIndex

svc = OSIndex(index="articles")

svc.create(mappings={
    "dynamic": "strict",
    "properties": {
        "title":      {"type": "text", "fields": {"raw": {"type": "keyword"}}},
        "body":       {"type": "text"},
        "created_at": {"type": "date"},
        "meta":       {"type": "object", "enabled": False},
    },
})

# 반영됐는지 확인
print(svc.get_mapping())
```

`OSDoc`나 `OSSearch`는 mapping에 관여하지 않는다. document를 ingest할 때
mapping 위반(`dynamic: strict`에서 미선언 field)이 있으면 bulk response의
`errors: true`로 드러나므로, bulk indexing 쪽에서도 반드시 error를 로깅한다.

## 9. mapping을 바꿔야 할 때 — reindex pattern

이미 선언된 field의 type이나 analyzer를 바꿀 수는 없다. 해결 방법은
정석적으로 reindex.

1. `articles_v2`라는 새 index를 목표 mapping으로 생성.
2. `POST _reindex` 로 `articles` → `articles_v2` 복사.
3. write alias를 `articles_v2`로 swap(`OSIndex.update_aliases`).
4. 검증 후 old index 삭제.

rollover alias를 이미 쓰고 있다면 더 쉽다. 새 write index에 새 mapping을
`create`할 때 지정하면 이후 rollover 이후 document부터 적용된다.
template(`_index_template`)을 관리하면 자동화할 수 있다.

## 10. 자주 하는 실수

- **string은 전부 text로 쓰고 있음.** keyword가 맞는 경우(ID, enum, tag)에
  text로 두면 tokenize되어 정렬/집계에 cost가 든다.
- **object로 매핑한 배열에 대해 "같은 element 조건 조합" 질의.** 평탄화
  때문에 cross-match가 생긴다. `nested`로 바꾸거나, 애초에 풀어서 저장.
- **leaf field에 `enabled: false`를 달려고 시도.** object에만 유효.
  keyword를 안 색인하고 싶다면 `index: false`.
- **`dynamic: true`를 production에 남겨 둠.** 오타 field가 mapping에 박혀
  되돌릴 수 없게 된다. `strict` 또는 `false`를 권장.
- **이미 선언된 field의 analyzer만 바꾸려 함.** 불가능. reindex 필요.
- **`text` field로 sort/aggs를 하려고 `fielddata: true`를 켬.** 가능은
  하지만 heap을 크게 먹는다. `.raw` keyword sub-field를 두는 것이 관용적.
- **한국어 문서에 `standard` analyzer.** 조사/어미 분석이 전혀 안 되어
  recall이 나쁘다. `nori` 같은 형태소 분석기가 필요하다.
- **mapping 변경을 `put_mapping`으로 시도.** 기존 field를 수정하려 하면
  거의 다 거부된다. 새 field **추가**만 가능하다.

## 11. mental model

> mapping은 index의 schema이고, 한 번 정하면 대부분 immutable이다.
> field type은 "무엇을 저장하는가"를, mapping parameter는 "어떻게 색인하는가"를 결정한다.
> `enabled: false`는 object subtree 전체를 불투명하게 만들고,
> `index: false`는 scalar를 저장하되 검색을 끊고,
> `keyword`는 문자열을 토큰화 없이 색인한다 — 이 셋은 목적이 다르다.
> `object`는 평탄화, `nested`는 독립 색인이다.
> dynamic mapping은 편하지만 오타를 영구히 박는다. production은 `strict` 또는 `false`.

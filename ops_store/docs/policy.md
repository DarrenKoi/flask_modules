# ISM policy 이해하기

이 문서는 OpenSearch ISM(Index State Management) policy의 구성 요소를
설명합니다. `ops_store/index.py`의 `OSIndex.create_ism_policy`,
`attach_ism_policy`, `delete_ism_policy`가 어떤 JSON 구조를 만들어 주는지,
그 구조 안의 **state**, **action**, **transition**이 무엇을 의미하는지
정리합니다.

manual rollover API 사용법은 `usage.md`의 "index가 커졌을 때 rollover"
section을 참고하세요. 이 문서는 "ISM이 자동으로 rollover와 retention을
돌리도록 policy를 어떻게 설계할까"에 집중합니다.

## 1. 개념 한 줄 정리

- **policy**는 하나의 index(또는 pattern으로 matching되는 여러 index)에
  붙는 state machine입니다.
- **state**는 이름과 action list, transition list로 구성된 노드입니다.
- **action**은 state에 진입했을 때 index에 적용할 변경입니다.
  (rollover, force_merge, delete 등)
- **transition**은 조건이 충족되면 index를 다른 state로 이동시키는 간선입니다.

## 2. hot / warm / cold / frozen / delete

OpenSearch ISM 입장에서 state 이름은 그냥 **문자열**입니다.
`"hot"`이든 `"banana"`든 동작에는 차이가 없습니다. 업계 관례로 데이터의
활동성을 나타내는 "온도" 용어를 씁니다.

| tier | 뜻 | 해당 state에서 보통 수행하는 action |
|---|---|---|
| **hot** | 활발히 write되고 query됨. 빠른 하드웨어, replica 많음. | `rollover`만 두는 경우가 많습니다 |
| **warm** | 더 이상 write하지 않지만 여전히 자주 query됨(최근 수 주). | `read_only`, `force_merge`, `replica_count` 감소, `allocation` |
| **cold** | 거의 query되지 않음. 감사/규정 목적으로 보관. 저장소가 저렴. | `replica_count: 0`, `allocation: cold`, 필요하면 `close` |
| **frozen** | 거의 열어 볼 일 없음. searchable snapshot으로 올려 둠. | `snapshot`, `allocation: frozen` |
| **delete** | 종단 state. index를 제거. | `delete` |

`ops_store`의 `create_ism_policy`는 **hot → delete**의 최소 두 단계 lifecycle만
생성합니다. 로그성 데이터처럼 retention window가 고정된 경우에 충분합니다.
four-tier(hot → warm → cold → delete)가 필요하면 직접 body를 만들어
`client.transport.perform_request`로 PUT 하거나 helper를 확장해야 합니다.

### 주의: node attribute가 없으면 allocation은 no-op

`allocation` action은 cluster의 data node들이 `node.attr.temp=hot|warm|cold`
같은 attribute tag를 가지고 있을 때만 물리적 이동을 만듭니다. tag가 없으면
state 이름만 의미가 있고, 실제로는 같은 노드 위에 그대로 머뭅니다.

## 3. state의 구조

state는 정확히 세 field로 구성됩니다.

```jsonc
{
  "name": "hot",          // policy 안에서 unique한 label
  "actions": [...],       // state에 진입할 때 위에서 아래로 실행
  "transitions": [...]    // action이 끝난 뒤 주기적으로 평가
}
```

state 안에서 일어나는 순서:

1. index가 state에 진입(policy attach 시 `default_state`로 들어오거나,
   다른 state의 transition이 fire되어 들어옴).
2. ISM이 `actions`를 순서대로 실행합니다. 대부분의 action은 한 번 실행되면
   끝이지만, `rollover`는 조건이 만족될 때까지 주기적으로 재평가됩니다.
3. 모든 action이 완료되면 ISM job scheduler가 `transitions`를 순서대로
   평가합니다(기본 주기 ~5분).
4. 첫 번째로 조건이 맞는 transition이 이깁니다. 그 state로 이동 후 다시 1부터.

## 4. action type reference

action은 key가 정확히 하나인 dictionary입니다. 그 key가 **action type**이고,
value는 해당 type의 parameter입니다.

자주 쓰는 action type:

| type | 설명 | 대표 parameter |
|---|---|---|
| `rollover` | write alias를 새 index로 넘김 | `min_index_age`, `min_size`, `min_doc_count`, `min_primary_shard_size` |
| `delete` | index 삭제 | 없음(`{}`) |
| `force_merge` | segment 수를 줄여 read를 빠르게 | `max_num_segments` |
| `read_only` | write 차단 | 없음 |
| `replica_count` | replica 수 변경 | `number_of_replicas` |
| `index_priority` | recovery priority 조정 | `priority` |
| `allocation` | shard를 특정 속성의 node로 이동 | `require`, `include`, `exclude` |
| `close` / `open` | index close/open | 없음 |
| `snapshot` | snapshot 생성 | `repository`, `snapshot` |
| `notification` | channel로 알림 전송 | `destination`, `message_template` |
| `shrink` | primary shard 수 감소 | `num_new_shards` 또는 `max_shard_size` |
| `rollup` | rollup job 실행 | `ism_rollup` spec |

모든 action은 선택적으로 `retry`, `timeout` wrapper를 함께 둘 수 있습니다.
이 wrapper들은 **action type key와 같은 level**에 둡니다.

```jsonc
{
  "retry":   {"count": 3, "backoff": "exponential", "delay": "1m"},
  "timeout": "1h",
  "rollover": {"min_primary_shard_size": "10gb"}
}
```

### rollover action의 특이점

`rollover`는 hot state에서 사실상 **gate** 역할을 합니다. 다른 action처럼
"한 번 실행하고 통과"하는 것이 아니라, 조건이 만족될 때까지 ISM job tick마다
재평가됩니다. 그래서 `create_ism_policy`에서 rollover는 transition 조건이
아니라 hot state의 action으로 들어가 있습니다.

## 5. transition과 condition

transition의 형태:

```jsonc
{
  "state_name": "warm",
  "conditions": { ... }   // 생략하면 "항상 true, 즉시 이동"
}
```

condition은 OR가 아니라 **AND**입니다. 하나의 transition에 여러 조건을 두면
모두 만족해야 fire됩니다. "A 또는 B"를 원하면 transition을 두 개로 나눠서
각각 조건 하나씩 걸면 됩니다(위에서 아래로 평가, first match wins).

자주 쓰는 condition key:

| condition | 의미 |
|---|---|
| `min_index_age` | index **생성** 시점 기준 경과 시간. rollover 후에는 새 index의 생성 시점이 기준이 되므로 값이 리셋됩니다. |
| `min_rollover_age` | index가 write alias에서 **밀려난** 시점 기준 경과 시간. hot 이후 tier 전환 조건으로 쓰기에 적합합니다. |
| `min_doc_count` | index의 doc 수가 임계치를 넘었는지 |
| `min_size` | primary store size가 임계치를 넘었는지 |
| `cron` | cron schedule에 맞춰 fire. 예: `{"cron": {"expression": "0 0 * * *", "timezone": "UTC"}}` |

### `min_index_age` vs `min_rollover_age`

warm/cold/delete로의 transition 조건에는 `min_rollover_age`를 쓰는 것이
안전합니다. `min_index_age`는 "index가 *생성*된 시점"부터 측정하므로, ingest
볼륨이 큰 hot index는 write alias를 들고 있는 동안에도 계속 나이가
먹습니다. rollover 직후 warm으로 넘기려 해도 "이미 30일 됐으니 바로
transition"이 되어 버려 warm에 머무는 시간이 없어질 수 있습니다.

## 6. `create_ism_policy`가 만드는 body 해설

`ops_store/index.py:245` helper를

```python
index_service.create_ism_policy(
    policy_id="articles-retention",
    index_pattern="articles-*",
    rollover_conditions={"min_primary_shard_size": "10gb", "min_index_age": "180d"},
    retention_age="365d",
    priority=100,
)
```

처럼 호출하면, 내부적으로 아래 JSON을 `PUT /_plugins/_ism/policies/articles-retention`
으로 보냅니다.

```jsonc
{
  "policy": {
    "description": "",
    "default_state": "hot",
    "states": [
      {
        "name": "hot",
        "actions": [
          {"rollover": {"min_primary_shard_size": "10gb", "min_index_age": "180d"}}
        ],
        "transitions": [
          {"state_name": "delete", "conditions": {"min_index_age": "365d"}}
        ]
      },
      {
        "name": "delete",
        "actions": [{"delete": {}}],
        "transitions": []
      }
    ],
    "ism_template": [
      {"index_patterns": ["articles-*"], "priority": 100}
    ]
  }
}
```

핵심 포인트:

- `default_state: "hot"`이므로 policy가 attach된 index는 hot에서 시작합니다.
- hot에 진입하면 `rollover` action이 걸립니다. 조건이 만족될 때까지 계속
  재평가되므로, 조건을 만족하기 전에는 transition 평가 자체가 시작되지
  않습니다.
- rollover가 완료되어 write alias가 새 index로 넘어가면, 이 index는 더 이상
  write를 받지 않습니다. 그 뒤부터 transition 조건(`min_index_age: 365d`)이
  반복 평가됩니다.
- `delete` state에 들어가면 `{"delete": {}}` action이 한 번 실행되고 index가
  제거됩니다.
- `ism_template`에 index pattern을 등록해 두었기 때문에, 이후 pattern에
  맞는 새 index가 생기면 policy가 **자동으로** 붙습니다. 기존 index에는
  붙지 않으므로 한 번은 `attach_ism_policy`를 명시적으로 호출해야 합니다.

## 7. attach / delete helper

`OSIndex.attach_ism_policy(policy_id, index)`는 이미 존재하는 index에 policy를
수동으로 붙입니다. rollover write index alias 쪽에 붙이면 alias가 가리키는
현재 write index에 적용됩니다.

`OSIndex.delete_ism_policy(policy_id)`는 policy 정의 자체를 삭제합니다. 이미
policy가 붙어 돌아가고 있는 index의 job을 멈추려면 `POST /_plugins/_ism/remove/<index>`
를 별도로 호출해야 합니다. policy만 지우면 job runtime이 orphan 상태로 남을
수 있습니다.

## 8. four-tier policy를 직접 만들기

`create_ism_policy`는 단순한 케이스만 커버합니다. hot/warm/cold/delete를
쓰고 싶다면 body를 직접 만들어 `self.client.transport.perform_request`로
PUT 하면 됩니다.

```python
body = {
    "policy": {
        "description": "articles 4-tier lifecycle",
        "default_state": "hot",
        "states": [
            {
                "name": "hot",
                "actions": [{"rollover": {"min_size": "50gb", "min_index_age": "7d"}}],
                "transitions": [
                    {"state_name": "warm", "conditions": {"min_rollover_age": "7d"}}
                ],
            },
            {
                "name": "warm",
                "actions": [
                    {"read_only": {}},
                    {"force_merge": {"max_num_segments": 1}},
                    {"replica_count": {"number_of_replicas": 1}},
                    {"allocation": {"require": {"temp": "warm"}}},
                ],
                "transitions": [
                    {"state_name": "cold", "conditions": {"min_rollover_age": "30d"}}
                ],
            },
            {
                "name": "cold",
                "actions": [
                    {"replica_count": {"number_of_replicas": 0}},
                    {"allocation": {"require": {"temp": "cold"}}},
                ],
                "transitions": [
                    {"state_name": "delete", "conditions": {"min_rollover_age": "365d"}}
                ],
            },
            {
                "name": "delete",
                "actions": [{"delete": {}}],
                "transitions": [],
            },
        ],
        "ism_template": [{"index_patterns": ["articles-*"], "priority": 100}],
    }
}

index_service.client.transport.perform_request(
    "PUT", "/_plugins/_ism/policies/articles-4tier", body=body
)
```

## 9. 자주 하는 실수

- **transition에 `min_index_age`만 사용**하고 hot 기간이 충분히 확보되지
  않는 경우. rollover 기준이 용량(`min_size`)이라면, 이후 tier 전환은
  `min_rollover_age`로 잡습니다.
- **rollover를 transition 조건으로 넣기.** rollover는 transition이 아니라
  **action**입니다. transition에 `min_size` 같은 조건을 걸어 "넘치면 다음
  state로"를 시도하면, rollover가 실제로 일어나지 않아 alias가 그대로 남고
  write가 같은 index로 계속 들어갑니다.
- **new index에만 auto-attach를 믿고 기존 index는 방치.** `ism_template`은
  pattern matching되는 **새 index**에만 policy를 자동으로 붙입니다. 이미
  존재하는 index에는 `attach_ism_policy`로 한 번 붙여야 합니다.
- **policy 수정 후 기존 index에 반영 안 됨.** policy를 PUT으로 덮어써도
  이미 job이 돌고 있는 index는 새 정의를 바로 반영하지 않습니다. 필요하면
  `POST /_plugins/_ism/change_policy/<index>`로 강제 교체합니다.
- **delete state에 transition을 둠.** terminal state입니다. transition은
  비워 둡니다.

## 10. mental model

> policy는 index에 붙는 state machine이다.
> state는 `(name, actions, transitions)` 3-tuple이다.
> action은 index를 바꾸고, transition은 index를 옮긴다.
> hot/warm/cold/delete는 **이름일 뿐**이며, 비용을 실제로 줄이는 것은 그
> state 안에 들어 있는 action들이다.

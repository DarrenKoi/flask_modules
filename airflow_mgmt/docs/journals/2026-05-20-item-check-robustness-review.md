# item_check_hourly 견고성 리뷰

작성일: 2026-05-20

## 논의 요약

처음 목표는 2일마다 갱신되는 약 50,000개 item list를 다음 갱신 전까지 한 번씩
check하는 방법을 정하는 것이었습니다. item list를 만드는 refresh schedule과 실제
item check schedule은 분리하기로 했고, Redis를 현재 generation과 item별 처리 상태의
저장소로 쓰기로 했습니다.

처리 방식은 item 50,000개를 Airflow task 50,000개로 펼치지 않고, 1시간마다 작은
batch를 처리하는 구조로 정했습니다. 최소 필요 처리량은 `50,000 / 48 = 약 1,042개/시간`
이지만, 운영 부하를 너무 키우지 않기 위해 기본값은 시간당 1,200개로 잡았습니다. 이 값은
4개 chunk task가 각각 300개씩 claim하는 방식입니다.

설계상 핵심 결정은 다음과 같습니다.

- 2일 refresh job은 새 Redis generation을 만들고 `current_generation`을 갱신합니다.
- hourly check DAG는 실행 시작 시점의 `current_generation`을 한 번 선택합니다.
- 각 chunk task는 같은 generation에서 최대 300개 item을 claim합니다.
- item list 자체는 XCom에 넣지 않고, XCom에는 generation과 summary만 남깁니다.
- 실제 item별 check/extract 로직은 `ITEM_CHECK_HANDLER=module:function`으로 연결합니다.

## 배경

`item_check_hourly`는 2일마다 갱신되는 item list와 별도로, 현재 Redis
generation에서 1시간마다 item을 가져와 check하는 DAG입니다. 기본 설정은 4개 chunk
task가 각각 300개씩 처리하여 시간당 1,200개 capacity를 만드는 구조입니다.

관련 파일:

- `airflow_mgmt/dags/item_check/item_check_dag.py`
- `airflow_mgmt/scripts/item_check_queue.py`
- `airflow_mgmt/tests/test_item_check_queue.py`

## 결론

현재 구현은 skeleton으로는 적절하지만, production scheduler로 바로 신뢰하기에는 아직
부족합니다. 단일 프로세스 테스트에서는 통과하지만 Redis 상태 전이가 여러 명령으로
쪼개져 있어, worker crash나 병렬 chunk task가 끼어드는 상황에서 item loss 또는 duplicate
processing이 발생할 수 있습니다.

## Findings

### High: claim 중간 crash 시 item loss 가능

`ItemCheckQueue.claim()`은 `pending` list에서 `LPOP`으로 item을 꺼낸 뒤, 별도 명령으로
`processing` sorted set에 lease deadline을 기록합니다.

위험 시나리오:

1. chunk task가 `pending`에서 item 300개를 pop합니다.
2. worker process가 죽거나 Redis 연결이 끊깁니다.
3. `processing`에 기록되기 전에 task가 종료됩니다.
4. 해당 item은 `pending`, `processing`, `done`, `failed` 어디에도 남지 않습니다.

조치:

- Redis Lua script로 "pending에서 최대 N개 pop + processing에 lease score 기록"을 하나의
  atomic operation으로 묶어야 합니다.
- 이 script는 claim된 item list를 반환하고, 실패하면 아무 상태도 바뀌지 않아야 합니다.

### High: expired reclaim이 병렬 chunk에서 duplicate를 만들 수 있음

`ItemCheckQueue.reclaim_expired()`는 `ZRANGEBYSCORE`, `ZREM`, `RPUSH`를 순서대로 호출합니다.
현재 DAG는 한 run에서 4개 chunk task를 병렬로 실행하므로, 두 task가 같은 expired item을
동시에 읽고 각각 `pending`에 다시 넣을 수 있습니다.

위험 시나리오:

1. `processing`에 expired item `A`가 있습니다.
2. `check_chunk_01`, `check_chunk_02`가 거의 동시에 `ZRANGEBYSCORE`를 호출합니다.
3. 둘 다 `A`를 expired로 봅니다.
4. 한 task가 `ZREM`에 성공하더라도 다른 task도 이미 `A`를 알고 있어 `RPUSH`할 수 있습니다.
5. `pending`에 `A`가 중복으로 들어가고, 같은 item이 여러 번 처리될 수 있습니다.

조치:

- reclaim도 Lua script로 "expired 조회 + 제거 + pending 재삽입"을 atomic하게 처리합니다.
- 더 단순한 대안은 reclaim 전용 task를 upstream에 하나만 두고, chunk task에서는 reclaim을
  하지 않는 것입니다. 그래도 Redis 명령 사이 crash를 줄이려면 Lua 방식이 더 안전합니다.

### Medium: Airflow task retry 설정이 없음

`item_check_hourly` DAG에는 현재 `retries`, `retry_delay` 같은 default retry 설정이 없습니다.
일시적인 Redis timeout, handler import 실패, 대상 시스템 network error가 있으면 그 시간대의
capacity가 그대로 사라집니다.

조치:

- DAG 또는 task에 retry를 설정합니다.
- 권장 기본값:
  - `retries=2`
  - `retry_delay=timedelta(minutes=5)`
  - Redis connection timeout은 현재처럼 짧게 유지합니다.

### Medium: generation loading이 50,000개 item 기준으로 낙관적임

`load_generation()`은 기존 generation key를 지운 뒤 전체 item을 한 번의 `RPUSH`로 넣고
`current_generation`을 갱신합니다. 50,000개 item이 작은 문자열이면 동작할 수 있지만, list
생성 중간 실패나 Redis command 크기 문제에 취약합니다.

조치:

- staging generation에 chunk 단위로 `RPUSH`합니다.
- 모든 item 적재와 meta 기록이 끝난 뒤에만 `current_generation`을 새 generation으로
  바꿉니다.
- 이전 generation은 새 generation 활성화가 끝난 뒤 별도 cleanup 대상으로 둡니다.

## Positive Notes

- `ITEM_CHECK_HANDLER`는 item claim 전에 load되므로, handler 설정 누락 때문에 item이
  `processing`에 묶이는 문제는 피했습니다.
- `select_generation()`이 hourly DAG run 시작 시 generation을 한 번 고정하고, chunk task는
  그 값을 사용합니다. 따라서 refresh 타이밍이 겹쳐도 하나의 hourly run이 old/new generation을
  섞어 처리하지 않습니다.
- XCom에는 item list를 넣지 않고 generation 및 summary만 넘기므로, Airflow metadata DB에
  큰 payload를 남기지 않습니다.

## 검증 상태

통과:

```bash
python -m pytest airflow_mgmt\tests\test_item_check_queue.py -v
```

결과: `7 passed`

통과:

```bash
python -m compileall airflow_mgmt\scripts\item_check_queue.py airflow_mgmt\dags\item_check
```

제한:

```bash
python -m pytest airflow_mgmt\tests -v
```

현재 local Python에는 `airflow` package가 없어 `test_dag_integrity.py` collection 단계에서
`ModuleNotFoundError: No module named 'airflow'`로 중단됩니다. repo `.venv`도 현재
`pytest`가 없어 full Airflow DAG integrity test를 실행하지 못했습니다.

## 다음 수정 우선순위

1. `claim()`과 `reclaim_expired()`를 Redis Lua script 기반 atomic operation으로 바꿉니다.
2. `item_check_hourly`에 retry 설정을 추가합니다.
3. `load_generation()`을 staging generation + chunked load + final activation 구조로 바꿉니다.
4. fake Redis 테스트에 race를 직접 재현하기는 어렵지만, Lua script 호출 contract 테스트와
   failure-path 테스트를 추가합니다.

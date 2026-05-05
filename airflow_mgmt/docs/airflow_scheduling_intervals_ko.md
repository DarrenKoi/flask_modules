# Airflow schedule과 cron 설정 가이드

Airflow에서 DAG 실행 시간을 정하는 핵심 인자는 `schedule`입니다. 현재 repo의
template처럼 Airflow 3 스타일에서는 `@dag(..., schedule=...)` 형태로 설정합니다.
예전 Airflow 문서나 코드에서는 `schedule_interval`이라는 이름도 보이지만, 새 DAG는
`schedule`을 기준으로 작성하는 편이 좋습니다.

## 가장 먼저 고를 것

스케줄은 보통 세 가지 중 하나로 고릅니다.

| 원하는 동작 | `schedule` 값 | 언제 쓰나 |
|---|---|---|
| 수동 실행만 허용 | `None` | 진단 DAG, 필요할 때만 누르는 작업 |
| 흔한 주기 | `"@hourly"`, `"@daily"` 등 preset | 매시간, 매일, 매주 같은 단순 주기 |
| wall-clock 기준 시간 | cron 문자열 | 매 2시간 정각, 매일 07:30, 평일 09:00 |
| 고정 duration 간격 | `datetime.timedelta(...)` | 시작 시점부터 2시간마다, 30분마다 |

대부분의 운영 DAG는 cron 문자열 또는 preset으로 충분합니다.

## 기본 코드 형태

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_hourly",
    start_date=datetime(2026, 1, 1),
    schedule="@hourly",
    catchup=False,
    max_active_runs=1,
    tags=["example"],
)
def example_hourly():
    @task
    def run() -> None:
        print("hello")

    run()


example_hourly()
```

실무 기본값으로는 `catchup=False`와 `max_active_runs=1`을 자주 둡니다.

- `catchup=False`: 과거에 놓친 interval을 한꺼번에 만들지 않습니다.
- `max_active_runs=1`: 이전 run이 아직 돌고 있으면 같은 DAG의 다음 run을 동시에
  시작하지 않습니다.

## 자주 쓰는 preset

| preset | 의미 | 같은 cron |
|---|---|---|
| `None` | 자동 schedule 없음, 수동/API trigger만 | 없음 |
| `"@once"` | 한 번만 실행 | 없음 |
| `"@continuous"` | 이전 run이 끝나면 바로 다음 run 생성 | 없음 |
| `"@hourly"` | 매시간 한 번 | `0 * * * *` |
| `"@daily"` | 매일 00:00 | `0 0 * * *` |
| `"@weekly"` | 매주 일요일 00:00 | `0 0 * * 0` |
| `"@monthly"` | 매월 1일 00:00 | `0 0 1 * *` |
| `"@quarterly"` | 매 분기 첫날 00:00 | `0 0 1 */3 *` |
| `"@yearly"` | 매년 1월 1일 00:00 | `0 0 1 1 *` |

`"@hourly"`는 가장 읽기 쉽습니다. 하지만 "2시간마다", "4시간마다", "평일만" 같은
조건은 cron으로 명확하게 쓰는 편이 좋습니다.

## 여러 DAG가 같은 시간에 뜨는 경우

`"@hourly"`는 `0 * * * *`와 같으므로, 여러 DAG가 모두 `"@hourly"`이면 매시 00분에
한꺼번에 실행 후보가 됩니다. Airflow가 worker slot, pool, DAG별 active run 제한을
무시하고 모두 동시에 실행하는 것은 아니지만, scheduler queue, worker, metadata DB,
그리고 OpenSearch, DB, FTP, MinIO 같은 외부 시스템에는 순간 부하가 생길 수 있습니다.

무거운 운영 DAG는 같은 정각에 몰아넣기보다 cron으로 분산합니다.

| 목적 | 예시 |
|---|---|
| 매시간 실행하되 정각 피하기 | `5 * * * *`, `15 * * * *`, `35 * * * *` |
| 같은 외부 시스템을 치는 task 제한 | Airflow pool 사용, task에 `pool="..."` 지정 |
| 같은 DAG의 겹치는 실행 방지 | `max_active_runs=1` |
| 과거 누락 run이 한꺼번에 생성되는 것 방지 | `catchup=False` |

task 안에서 `sleep()`으로 시간을 미루면 worker slot을 붙잡은 채 대기하게 됩니다. 단순히
시작 시점을 나누려는 목적이면 task 내부 대기보다 cron minute을 다르게 두는 편이 좋습니다.

```python
@dag(
    dag_id="example_staggered_hourly",
    start_date=datetime(2026, 1, 1),
    schedule="15 * * * *",
    catchup=False,
    max_active_runs=1,
    max_active_tasks=2,
)
def example_staggered_hourly():
    @task(pool="shared_opensearch_pool")
    def run() -> None:
        ...

    run()
```

## cron 형식

Airflow cron은 일반적인 5칸 형식입니다.

```text
분 시 일 월 요일
```

예를 들어 `0 */2 * * *`는 다음처럼 읽습니다.

```text
0분 / 2시간마다 / 매일 / 매월 / 모든 요일
```

즉 00:00, 02:00, 04:00, 06:00처럼 실행됩니다.

## 자주 쓰는 cron 예시

| 원하는 schedule | cron | 설명 |
|---|---|---|
| 매시간 정각 | `0 * * * *` | 00분마다 |
| 매시간 10분 | `10 * * * *` | 00:10, 01:10, 02:10 |
| 30분마다 | `*/30 * * * *` | 매시 00분, 30분 |
| 15분마다 | `*/15 * * * *` | 매시 00, 15, 30, 45분 |
| 2시간마다 | `0 */2 * * *` | 00, 02, 04, 06시 |
| 4시간마다 | `0 */4 * * *` | 00, 04, 08, 12, 16, 20시 |
| 6시간마다 | `0 */6 * * *` | 00, 06, 12, 18시 |
| 매일 07:30 | `30 7 * * *` | 매일 아침 7시 30분 |
| 매일 23:00 | `0 23 * * *` | 매일 밤 11시 |
| 평일 09:00 | `0 9 * * MON-FRI` | 월요일부터 금요일 |
| 토요일 03:00 | `0 3 * * SAT` | 매주 토요일 |
| 매월 1일 00:00 | `0 0 1 * *` | 월간 batch |
| 매월 1일과 15일 02:00 | `0 2 1,15 * *` | 월 2회 |
| 1월마다 매일 01:00 | `0 1 * 1 *` | 특정 월 제한 |

## 2시간마다와 4시간마다

가장 흔한 질문은 "every two hours"와 "every four hours"입니다.

```python
@dag(
    dag_id="example_every_two_hours",
    start_date=datetime(2026, 1, 1),
    schedule="0 */2 * * *",
    catchup=False,
)
def example_every_two_hours():
    ...
```

```python
@dag(
    dag_id="example_every_four_hours",
    start_date=datetime(2026, 1, 1),
    schedule="0 */4 * * *",
    catchup=False,
)
def example_every_four_hours():
    ...
```

`*/2`와 `*/4`는 "현재 run이 끝난 뒤 2시간/4시간 대기"가 아니라 wall-clock hour를
나누는 표현입니다. 따라서 `0 */4 * * *`는 00, 04, 08, 12, 16, 20시에 맞춰집니다.

만약 01, 05, 09, 13, 17, 21시에 돌리고 싶다면 다음처럼 씁니다.

```python
schedule="0 1-23/4 * * *"
```

## cron과 `timedelta` 차이

둘 다 interval을 만들지만 기준이 다릅니다.

| 방식 | 예시 | 기준 |
|---|---|---|
| cron | `"0 */2 * * *"` | 시계 시간, 00/02/04시 같은 경계 |
| `timedelta` | `timedelta(hours=2)` | `start_date`부터 계산한 duration |

`timedelta`를 쓰려면 다음처럼 작성합니다.

```python
from datetime import datetime, timedelta

from airflow.sdk import dag


@dag(
    dag_id="example_timedelta_two_hours",
    start_date=datetime(2026, 1, 1, 1, 30),
    schedule=timedelta(hours=2),
    catchup=False,
)
def example_timedelta_two_hours():
    ...
```

이 경우 기준은 `start_date`입니다. 위 예시는 01:30을 기준으로 2시간 간격을 만듭니다.
운영자가 UI에서 봤을 때 이해하기 쉬운 것은 보통 cron입니다. "정각마다", "매일 7시
30분"처럼 설명되는 작업은 cron을 우선 고려합니다.

## Airflow run 시간과 data interval

Airflow의 scheduled DAG run은 "그 시간에 처리할 데이터 구간"을 갖습니다. 예를 들어
`@hourly` DAG는 10:00 run이 보통 09:00부터 10:00까지의 data interval을 대표합니다.

중요한 점은 다음과 같습니다.

- Airflow는 interval이 끝난 뒤 그 interval의 DAG run을 만듭니다.
- `logical_date`는 실제 시작 시간이 아니라 data interval의 기준 시간으로 이해합니다.
- manual trigger는 schedule과 별개로 사용자가 누른 시점의 run을 만듭니다.

그래서 task 코드에서 "지금 시간"만 보고 데이터를 가져오면 backfill이나 재실행에서
틀어질 수 있습니다. 가능하면 Airflow context의 `data_interval_start`와
`data_interval_end`를 기준으로 처리 구간을 정합니다.

```python
from airflow.sdk import get_current_context, task


@task
def print_interval() -> None:
    context = get_current_context()
    print(context["data_interval_start"])
    print(context["data_interval_end"])
```

## `start_date` 설정

`start_date`는 고정된 과거 날짜로 둡니다.

좋은 예:

```python
start_date=datetime(2026, 1, 1)
```

피해야 할 예:

```python
start_date=datetime.now()
```

`datetime.now()`처럼 parse할 때마다 바뀌는 값은 scheduler가 안정적으로 다음 run을
계산하기 어렵게 만듭니다.

## `catchup` 설정

`catchup=True`이면 `start_date`부터 현재까지 누락된 모든 interval의 DAG run을 만들 수
있습니다. 데이터 backfill 목적이면 유용하지만, 운영 job에서는 원치 않는 대량 실행이
될 수 있습니다.

일반 운영 DAG는 보통 다음처럼 시작합니다.

```python
catchup=False
```

과거 날짜를 의도적으로 다시 처리해야 할 때만 Airflow UI나 CLI의 backfill/re-run
기능을 별도로 사용합니다.

## timezone

회사 Airflow의 기본 timezone이 UTC인지 Asia/Seoul인지 먼저 확인해야 합니다. cron의
07:30이 한국 시간인지 UTC 07:30인지가 달라지기 때문입니다.

한국 업무 시간 기준이 중요하면 timezone-aware `start_date`를 사용하는 방식을 검토합니다.

```python
import pendulum

from airflow.sdk import dag


@dag(
    dag_id="example_kst_daily",
    start_date=pendulum.datetime(2026, 1, 1, tz="Asia/Seoul"),
    schedule="30 7 * * *",
    catchup=False,
)
def example_kst_daily():
    ...
```

다만 회사 플랫폼에서 timezone 정책을 이미 정해 두었다면 그 정책을 따릅니다. 같은 repo
안에서 DAG마다 timezone 해석이 달라지면 운영자가 UI에서 시간을 해석하기 어려워집니다.

## 겹치는 실행 방지

스케줄이 짧고 task가 오래 걸리면 이전 run이 끝나기 전에 다음 run이 시작될 수 있습니다.
이를 피하려면 DAG에 `max_active_runs=1`을 둡니다.

```python
@dag(
    dag_id="example_no_overlap",
    start_date=datetime(2026, 1, 1),
    schedule="0 */2 * * *",
    catchup=False,
    max_active_runs=1,
)
def example_no_overlap():
    ...
```

task 자체의 병렬 실행 수를 더 제한해야 하면 task-level concurrency, pool, queue 같은
운영 설정도 검토합니다. 처음에는 DAG 단위 `max_active_runs=1`이 가장 이해하기 쉽습니다.

## 빠른 선택표

| 원하는 말 | 추천 설정 |
|---|---|
| 수동으로만 실행 | `schedule=None` |
| 매시간 | `schedule="@hourly"` |
| 매일 자정 | `schedule="@daily"` |
| 매일 07:30 | `schedule="30 7 * * *"` |
| 2시간마다 정각 | `schedule="0 */2 * * *"` |
| 4시간마다 정각 | `schedule="0 */4 * * *"` |
| 30분마다 | `schedule="*/30 * * * *"` |
| 평일 아침 9시 | `schedule="0 9 * * MON-FRI"` |
| 이전 run 끝나면 계속 | `schedule="@continuous"` |
| 시작 시점 기준 2시간 간격 | `schedule=timedelta(hours=2)` |

## 참고 공식 문서

- Cron & Time Intervals: <https://airflow.apache.org/docs/apache-airflow/stable/authoring-and-scheduling/cron.html>
- DAG schedule와 data interval: <https://airflow.apache.org/docs/apache-airflow/3.1.5/core-concepts/dags.html#running-dags>

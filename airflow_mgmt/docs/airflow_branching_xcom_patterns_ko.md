# Airflow 분기와 XCom 패턴

Airflow에서 XCom은 task 사이에 작은 runtime 값을 전달하는 장치입니다. TaskFlow
스타일에서는 `@task` 함수의 return value가 자동으로 XCom에 저장되고, 그 값을 다른
`@task`의 인자로 넘기면 Airflow가 dependency와 pull을 처리합니다.

## XCom으로 할 수 있는 것과 하면 안 되는 것

| 질문 | 답 |
|---|---|
| upstream 결과에 따라 다른 task를 실행할 수 있나? | 가능합니다. `@task.branch`를 사용합니다. |
| upstream 결과가 false면 downstream을 skip할 수 있나? | 가능합니다. `@task.short_circuit`를 사용합니다. |
| upstream 결과 list 개수만큼 task를 만들 수 있나? | 가능합니다. dynamic task mapping의 `.expand()`를 사용합니다. |
| XCom 값으로 함수 인자를 바꿀 수 있나? | 가능합니다. TaskFlow 인자 전달을 사용합니다. |
| XCom 값으로 DAG Python 코드를 runtime에 새로 작성할 수 있나? | 권장하지 않습니다. DAG 구조는 parse time에 정의합니다. |
| 큰 파일, DataFrame, binary payload를 XCom에 넣어도 되나? | 피해야 합니다. MinIO/S3, DB, file path/object key를 전달합니다. |

XCom은 "작은 metadata"용입니다. 예를 들어 row count, object key, batch id, 검증
결과 summary, 다음 branch 이름 정도가 적합합니다.

## TaskFlow return value로 XCom 전달하기

가장 좋은 기본 패턴입니다. `inspect_input()`이 반환한 dict가 XCom이 되고,
`load_data()`는 그 dict를 인자로 받습니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_taskflow_xcom",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_taskflow_xcom():
    @task
    def inspect_input() -> dict:
        return {
            "object_key": "raw/orders_2026_01_01.csv",
            "row_count": 1200,
            "has_error": False,
        }

    @task
    def load_data(summary: dict) -> None:
        print(f"loading {summary['object_key']} rows={summary['row_count']}")

    summary = inspect_input()
    load_data(summary)


example_taskflow_xcom()
```

classic `PythonOperator`에서도 return value는 기본적으로 `return_value` key의 XCom에
저장됩니다. 다만 직접 `ti.xcom_pull()`을 써야 하므로 새 DAG는 TaskFlow 스타일이 더
읽기 쉽습니다.

## XCom 결과로 branch 결정하기

`@task.branch` 함수는 실행할 downstream `task_id`를 반환합니다. 선택되지 않은
직접 downstream task들은 `skipped`가 됩니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_branch_by_xcom",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_branch_by_xcom():
    @task
    def inspect_input() -> dict:
        return {"row_count": 0, "error_count": 0}

    @task.branch
    def choose_path(summary: dict) -> str:
        if summary["error_count"] > 0:
            return "quarantine"
        if summary["row_count"] == 0:
            return "no_data"
        return "load_data"

    @task
    def no_data() -> None:
        print("nothing to load")

    @task
    def quarantine() -> None:
        print("move bad input to quarantine area")

    @task
    def load_data() -> None:
        print("load data")

    @task(trigger_rule="none_failed_min_one_success")
    def finish() -> None:
        print("selected path finished")

    summary = inspect_input()
    route = choose_path(summary)

    no_data_task = no_data()
    quarantine_task = quarantine()
    load_task = load_data()
    finish_task = finish()

    route >> [no_data_task, quarantine_task, load_task]
    [no_data_task, quarantine_task, load_task] >> finish_task


example_branch_by_xcom()
```

`finish()`에 `trigger_rule="none_failed_min_one_success"`를 둔 이유는 branch에서
선택되지 않은 task들이 `skipped`가 되기 때문입니다. 기본값 `all_success`를 쓰면
선택된 path가 성공해도 join task가 skip될 수 있습니다.

## True/False만 필요하면 short-circuit

분기 path가 여러 개가 아니라 "이후 작업을 계속할지 말지"만 결정하면
`@task.short_circuit`이 더 단순합니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_short_circuit",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_short_circuit():
    @task
    def inspect_input() -> dict:
        return {"row_count": 0}

    @task.short_circuit
    def has_rows(summary: dict) -> bool:
        return summary["row_count"] > 0

    @task
    def load_data(summary: dict) -> None:
        print(f"loading rows={summary['row_count']}")

    summary = inspect_input()
    gate = has_rows(summary)
    gate >> load_data(summary)


example_short_circuit()
```

`has_rows()`가 `False`를 반환하면 downstream task는 실행되지 않고 `skipped`가 됩니다.

## 직접 XCom push/pull이 필요한 경우

대부분은 TaskFlow return value로 충분합니다. 직접 key를 나누어 저장해야 할 때만
`get_current_context()`로 task context를 가져와 `ti.xcom_push()`와 `ti.xcom_pull()`을
사용합니다.

```python
from datetime import datetime

from airflow.sdk import dag, get_current_context, task


@dag(
    dag_id="example_manual_xcom",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_manual_xcom():
    @task
    def prepare() -> None:
        context = get_current_context()
        ti = context["ti"]
        ti.xcom_push(key="batch_id", value="orders_20260101")
        ti.xcom_push(key="object_key", value="raw/orders_20260101.csv")

    @task
    def consume() -> None:
        context = get_current_context()
        ti = context["ti"]
        batch_id = ti.xcom_pull(task_ids="prepare", key="batch_id")
        object_key = ti.xcom_pull(task_ids="prepare", key="object_key")
        print(f"batch_id={batch_id} object_key={object_key}")

    prepare() >> consume()


example_manual_xcom()
```

직접 XCom key를 늘리면 schema 관리가 어려워질 수 있습니다. 가능한 경우에는 dict
하나를 return하고 downstream에서 명시적으로 읽는 편이 더 단순합니다.

## XCom list로 dynamic task mapping 하기

입력 개수가 runtime에 결정될 때는 `.expand()`를 사용합니다. upstream task가 list를
반환하면 scheduler가 각 원소마다 mapped task instance를 만듭니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_dynamic_mapping_from_xcom",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_dynamic_mapping_from_xcom():
    @task
    def list_object_keys() -> list[str]:
        return [
            "raw/orders_1.csv",
            "raw/orders_2.csv",
            "raw/orders_3.csv",
        ]

    @task
    def process_one(object_key: str) -> dict:
        return {"object_key": object_key, "status": "ok"}

    @task
    def summarize(results: list[dict]) -> None:
        print(results)

    object_keys = list_object_keys()
    results = process_one.expand(object_key=object_keys)
    summarize(results)


example_dynamic_mapping_from_xcom()
```

이 방식은 "파일이 몇 개인지 DAG 작성 시점에는 모르지만, 실행 시점에 list를 보고 같은
작업을 반복"해야 할 때 적합합니다.

## 설계 기준

- XCom에는 작은 JSON 직렬화 가능 값을 둡니다.
- 큰 데이터는 MinIO/S3, DB, file path, object key로 넘깁니다.
- branch 함수는 실행할 `task_id`를 명확히 반환합니다.
- branch 뒤 join task에는 `none_failed` 또는 `none_failed_min_one_success`를 검토합니다.
- runtime 조건이 "여러 path 중 하나"이면 branch를 씁니다.
- runtime 조건이 "계속/중단"이면 short-circuit을 씁니다.
- runtime 개수가 달라지는 반복이면 dynamic task mapping을 씁니다.
- DAG 파일 자체의 구조는 parse time에 결정된다고 생각합니다.

## 참고 공식 문서

- XCom 설명: <https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/xcoms.html>
- BranchPythonOperator와 `@task.branch`: <https://airflow.apache.org/docs/apache-airflow-providers-standard/stable/operators/python.html#branchpythonoperator>
- ShortCircuitOperator와 `@task.short_circuit`: <https://airflow.apache.org/docs/apache-airflow/2.10.4/howto/operator/python.html#shortcircuitoperator>
- Dynamic task mapping 가이드: <https://airflow.apache.org/docs/apache-airflow/3.1.5/authoring-and-scheduling/dynamic-task-mapping.html>

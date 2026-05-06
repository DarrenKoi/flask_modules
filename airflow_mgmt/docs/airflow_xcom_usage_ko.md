# Airflow XCom 설정과 사용 방법

XCom은 Airflow task 사이에서 작은 runtime 값을 주고받기 위한 기능입니다. 이름은
"cross-communication"에서 왔고, 보통 upstream task의 결과 summary를 downstream task가
읽을 때 사용합니다.

## 먼저 기억할 원칙

| 원칙 | 설명 |
|---|---|
| 작은 값만 전달 | row count, object key, batch id, status dict 정도가 적합합니다. |
| 큰 데이터는 외부 저장소로 | DataFrame, 파일 내용, binary는 MinIO/S3, DB, 파일 경로로 넘깁니다. |
| `@task` return을 우선 사용 | TaskFlow 스타일에서는 return value가 자동 XCom이 됩니다. |
| 직접 push/pull은 필요할 때만 | key를 나눠야 하거나 classic operator와 연결할 때 사용합니다. |
| 실패한 task의 XCom은 없을 수 있음 | task가 exception으로 끝나면 return value XCom이 생성되지 않습니다. |

## 방법 1: `@task` return value로 자동 set

새 DAG에서는 이 방식이 가장 읽기 쉽습니다. `prepare()`가 반환한 dict는 자동으로
XCom에 저장되고, `consume(summary)`는 그 값을 자동으로 받습니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_xcom_return_value",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_xcom_return_value():
    @task
    def prepare() -> dict:
        return {
            "batch_id": "orders_20260101",
            "object_key": "raw/orders_20260101.csv",
            "row_count": 1200,
        }

    @task
    def consume(summary: dict) -> None:
        print(summary["batch_id"])
        print(summary["object_key"])
        print(summary["row_count"])

    summary = prepare()
    consume(summary)


example_xcom_return_value()
```

이때 Airflow가 내부적으로 하는 일은 다음과 같습니다.

1. `prepare` task가 실행됩니다.
2. return dict가 XCom의 기본 key인 `return_value`에 저장됩니다.
3. `consume` task가 실행될 때 Airflow가 그 값을 pull해서 인자로 넣습니다.

## 방법 2: 직접 `xcom_push()`로 set

여러 key를 명시적으로 나눠 저장하고 싶을 때는 `get_current_context()`로 현재 task
context를 가져온 뒤 `ti.xcom_push()`를 사용합니다.

```python
from datetime import datetime

from airflow.sdk import dag, get_current_context, task


@dag(
    dag_id="example_xcom_push",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_xcom_push():
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

        print(f"batch_id={batch_id}")
        print(f"object_key={object_key}")

    prepare() >> consume()


example_xcom_push()
```

직접 key를 많이 만들면 downstream에서 어떤 key가 필요한지 추적하기 어려워집니다.
가능하면 dict 하나를 return하는 방식부터 검토합니다.

## 방법 3: dict를 여러 XCom key로 나누기

`multiple_outputs=True`를 사용하면 dict의 각 key가 별도 XCom key로 저장됩니다.

```python
from datetime import datetime

from airflow.sdk import dag, get_current_context, task


@dag(
    dag_id="example_xcom_multiple_outputs",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_xcom_multiple_outputs():
    @task(multiple_outputs=True)
    def prepare() -> dict:
        return {
            "batch_id": "orders_20260101",
            "object_key": "raw/orders_20260101.csv",
        }

    @task
    def consume() -> None:
        context = get_current_context()
        ti = context["ti"]

        batch_id = ti.xcom_pull(task_ids="prepare", key="batch_id")
        object_key = ti.xcom_pull(task_ids="prepare", key="object_key")
        print(batch_id, object_key)

    prepare() >> consume()


example_xcom_multiple_outputs()
```

이 방식은 key별 pull이 꼭 필요할 때만 씁니다. 단순한 TaskFlow chaining이라면
`multiple_outputs=True` 없이 dict를 그대로 넘겨도 충분합니다.

## 방법 4: classic `PythonOperator`에서 pull

이 repo는 새 DAG에 TaskFlow를 우선 권장하지만, classic operator를 만날 수도 있습니다.
`PythonOperator` callable은 Airflow context에서 `ti`를 받아 XCom을 pull할 수 있습니다.

```python
from datetime import datetime

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG


def prepare(**context) -> dict:
    return {"batch_id": "orders_20260101"}


def consume(**context) -> None:
    ti = context["ti"]
    summary = ti.xcom_pull(task_ids="prepare")
    print(summary["batch_id"])


with DAG(
    dag_id="example_python_operator_xcom",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
) as dag:
    prepare_task = PythonOperator(
        task_id="prepare",
        python_callable=prepare,
    )

    consume_task = PythonOperator(
        task_id="consume",
        python_callable=consume,
    )

    prepare_task >> consume_task
```

`xcom_pull(task_ids="prepare")`처럼 key를 생략하면 기본 key인 `return_value`를 읽습니다.

## XCom으로 branch 결정하기

XCom 값은 "어떤 코드를 실행할지"가 아니라 "어떤 task path를 실행할지" 결정하는 데
사용합니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_xcom_branch",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_xcom_branch():
    @task
    def inspect() -> dict:
        return {"row_count": 0}

    @task.branch
    def choose(summary: dict) -> str:
        if summary["row_count"] == 0:
            return "skip_load"
        return "load_data"

    @task
    def skip_load() -> None:
        print("no rows")

    @task
    def load_data() -> None:
        print("load rows")

    summary = inspect()
    route = choose(summary)
    route >> [skip_load(), load_data()]


example_xcom_branch()
```

branch 함수는 실행할 downstream `task_id`를 반환합니다. 선택되지 않은 downstream task는
`skipped`가 됩니다.

## XCom list로 여러 task 실행하기

upstream task가 list를 반환하면 `.expand()`로 item마다 같은 task를 실행할 수 있습니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_xcom_dynamic_mapping",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_xcom_dynamic_mapping():
    @task
    def list_files() -> list[str]:
        return ["raw/a.csv", "raw/b.csv", "raw/c.csv"]

    @task
    def process_file(object_key: str) -> dict:
        return {"object_key": object_key, "status": "ok"}

    @task
    def summarize(results: list[dict]) -> None:
        print(results)

    files = list_files()
    results = process_file.expand(object_key=files)
    summarize(results)


example_xcom_dynamic_mapping()
```

이 패턴은 "실행해 봐야 파일 개수를 알 수 있다"는 상황에 적합합니다.

## Jinja template에서 XCom 읽기

template field를 가진 operator에서는 Jinja로 XCom을 읽을 수도 있습니다. 예를 들어
SQL이나 shell command 안에 upstream 결과를 넣어야 할 때 사용합니다.

```python
bash_command = """
echo {{ ti.xcom_pull(task_ids='prepare', key='object_key') }}
"""
```

다만 Python task끼리는 Jinja보다 TaskFlow 인자 전달이 더 안전하고 읽기 쉽습니다.

## UI에서 확인하기

Airflow UI에서는 DAG run의 task instance 상세 화면에서 XCom 값을 확인할 수 있습니다.
운영 중에는 다음 순서로 확인합니다.

1. DAG run을 엽니다.
2. upstream task가 `success`인지 확인합니다.
3. task instance 상세에서 XCom 또는 rendered template 영역을 확인합니다.
4. downstream task log에서 pull한 값이 기대와 같은지 확인합니다.

## 흔한 문제

| 증상 | 원인 | 해결 |
|---|---|---|
| downstream에서 `None`이 나옴 | `task_ids` 또는 `key`가 틀림 | 실제 task id와 key를 확인합니다. |
| downstream에서 `None`이 나옴 | upstream이 실패해서 XCom을 못 남김 | 실패 path에서는 XCom 의존을 줄입니다. |
| serialization error | return value가 직렬화 불가능 | dict/list/str/int/bool 같은 단순 값으로 바꿉니다. |
| metadata DB가 커짐 | 큰 payload를 XCom에 저장 | object storage에 저장하고 key만 XCom으로 넘깁니다. |
| branch 후 join이 skip됨 | join task의 trigger rule이 `all_success` | `none_failed` 또는 `none_failed_min_one_success`를 검토합니다. |
| TaskGroup 안에서 task id가 안 맞음 | task id에 group prefix가 붙음 | UI의 실제 task id를 기준으로 pull합니다. |

## 이 repo에서 권장하는 사용 방식

1. 새 DAG는 `airflow_mgmt/dag_templates/taskflow_decorator_template.py`를 기본 출발점으로 사용합니다.
2. `@task` return value로 작은 dict를 넘깁니다.
3. 큰 데이터는 `minio_handler/`로 object storage에 쓰고 object key만 XCom에 둡니다.
4. 조건부 실행은 `@task.branch` 또는 `@task.short_circuit`로 표현합니다.
5. 직접 `xcom_push()`와 `xcom_pull()`은 classic operator나 명시적 key가 필요할 때만 씁니다.

## 참고 공식 문서

- XComs: <https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/xcoms.html>
- Airflow 3 public interface: <https://airflow.apache.org/docs/apache-airflow/stable/public-airflow-interface.html>
- Dynamic task mapping: <https://airflow.apache.org/docs/apache-airflow/3.1.5/authoring-and-scheduling/dynamic-task-mapping.html>

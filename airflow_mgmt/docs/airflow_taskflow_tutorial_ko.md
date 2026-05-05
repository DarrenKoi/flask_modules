# Airflow TaskFlow 튜토리얼

TaskFlow는 Airflow에서 Python 중심 DAG를 더 짧고 읽기 쉽게 작성하는 방식입니다.
일반 Python 함수에 `@task`를 붙이면 Airflow task가 되고, 함수 return value는 자동으로
XCom에 저장되어 downstream task로 전달됩니다.

이 repo는 Airflow 3 계열을 기준으로 하므로 새 DAG 예시는 다음 import를 사용합니다.

```python
from airflow.sdk import dag, task
```

Airflow 2 문서나 예전 코드에서는 `from airflow.decorators import dag, task`를 볼 수
있습니다. Airflow 3에서는 DAG author용 public interface인 `airflow.sdk`를 우선 사용합니다.

## TaskFlow가 해결하는 문제

classic `PythonOperator` 방식에서는 Python 함수를 task로 감싸고, dependency를 직접
연결하고, XCom을 직접 pull하는 코드가 자주 필요했습니다.

TaskFlow에서는 다음을 Airflow가 자동으로 처리합니다.

| 항목 | classic style | TaskFlow style |
|---|---|---|
| Python task 생성 | `PythonOperator(...)` | `@task` |
| DAG 생성 | `with DAG(...)` 또는 `DAG(...)` | `@dag` |
| task id | `task_id` 직접 지정 | 기본값은 함수 이름 |
| return value 전달 | `ti.xcom_pull(...)` | 함수 인자처럼 전달 |
| dependency | `task_a >> task_b` | 함수 호출 관계로 자동 연결 |
| 적합한 작업 | operator 중심 workflow | Python function 중심 workflow |

TaskFlow는 Airflow 2.0에서 도입되었습니다. Airflow 1.x 스타일 DAG를 보면
`PythonOperator`와 `xcom_pull()`이 더 자주 보입니다.

## 가장 작은 TaskFlow DAG

아래 DAG는 수동 실행용입니다. `schedule=None`이므로 Airflow UI나 API에서 trigger할 때만
실행됩니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_taskflow_minimal",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["example"],
)
def example_taskflow_minimal():
    @task
    def hello() -> None:
        print("hello taskflow")

    hello()


example_taskflow_minimal()
```

핵심은 마지막의 `example_taskflow_minimal()` 호출입니다. `@dag`가 붙은 함수를 호출해야
Airflow가 DAG 객체를 만들고 발견할 수 있습니다.

## 함수 호출이 실제 실행은 아니다

TaskFlow에서 가장 헷갈리는 부분은 이것입니다.

```python
result = hello()
```

이 코드는 DAG parse 시점에 `hello()` 함수 본문을 실행하는 것이 아닙니다. Airflow task를
정의하고, 그 task의 미래 결과를 가리키는 객체를 반환합니다. 이 객체를 보통 `XComArg`로
이해하면 됩니다.

즉 DAG 파일을 읽을 때는 workflow graph만 만들고, 함수 본문은 scheduler가 task instance를
실행할 때 worker에서 실행됩니다.

## ETL 예제로 이해하기

TaskFlow는 `extract -> transform -> load` 같은 Python ETL 흐름에서 가장 자연스럽습니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_taskflow_etl",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["example", "taskflow"],
)
def example_taskflow_etl():
    @task
    def extract() -> dict[str, int]:
        return {
            "A100": 10,
            "A200": 25,
            "A300": 5,
        }

    @task
    def transform(rows: dict[str, int]) -> dict[str, int]:
        total_qty = sum(rows.values())
        return {
            "item_count": len(rows),
            "total_qty": total_qty,
        }

    @task
    def load(summary: dict[str, int]) -> None:
        print(f"item_count={summary['item_count']}")
        print(f"total_qty={summary['total_qty']}")

    raw_rows = extract()
    summary = transform(raw_rows)
    load(summary)


example_taskflow_etl()
```

`extract()`의 return value는 XCom으로 저장됩니다. `transform(raw_rows)`라고 쓰면
Airflow가 `extract -> transform` dependency를 자동으로 만듭니다. 같은 방식으로
`load(summary)`는 `transform -> load` dependency를 만듭니다.

## 같은 DAG를 classic style로 쓰면

비교를 위해 같은 구조를 classic `PythonOperator`로 쓰면 다음처럼 됩니다.

```python
from datetime import datetime

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG


def extract() -> dict[str, int]:
    return {"A100": 10, "A200": 25, "A300": 5}


def transform(**context) -> dict[str, int]:
    ti = context["ti"]
    rows = ti.xcom_pull(task_ids="extract")
    return {
        "item_count": len(rows),
        "total_qty": sum(rows.values()),
    }


def load(**context) -> None:
    ti = context["ti"]
    summary = ti.xcom_pull(task_ids="transform")
    print(summary)


with DAG(
    dag_id="example_classic_python_operator_etl",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
) as dag:
    extract_task = PythonOperator(
        task_id="extract",
        python_callable=extract,
    )
    transform_task = PythonOperator(
        task_id="transform",
        python_callable=transform,
    )
    load_task = PythonOperator(
        task_id="load",
        python_callable=load,
    )

    extract_task >> transform_task >> load_task
```

동작은 비슷하지만, `PythonOperator` 생성, `task_id`, `xcom_pull`, `>>` 연결을 직접 써야
합니다. Python 함수 중심 DAG라면 TaskFlow가 더 단순합니다.

## XComArg를 함수 인자로 넘기기

TaskFlow에서는 upstream 결과를 일반 함수 인자처럼 넘깁니다.

```python
raw_rows = extract()
summary = transform(raw_rows)
load(summary)
```

여기서 `raw_rows`와 `summary`는 실제 dict가 아니라 runtime에 dict가 될 XCom reference입니다.
따라서 DAG 정의 시점에 다음처럼 값을 직접 검사하면 안 됩니다.

```python
# 잘못된 패턴입니다.
if summary["total_qty"] > 0:
    load(summary)
```

조건 판단은 DAG parse 시점이 아니라 task 실행 시점에 해야 합니다. 이런 경우
`@task.branch`나 `@task.short_circuit`을 사용합니다.

## 여러 값을 나눠 넘기기

return dict를 downstream에서 통째로 받는 것이 기본입니다.

```python
@task
def summarize() -> dict:
    return {"ok": 10, "ng": 2}


@task
def report(summary: dict) -> None:
    print(summary["ok"])
```

dict key를 각각 별도 XCom key처럼 다루고 싶다면 `multiple_outputs=True`를 사용할 수
있습니다.

```python
@task(multiple_outputs=True)
def summarize() -> dict:
    return {"ok": 10, "ng": 2}
```

단순한 DAG에서는 dict 전체를 넘기는 방식이 더 명확합니다. key별 pull이 필요한 경우에만
`multiple_outputs=True`를 검토합니다.

## retry와 timeout 설정

TaskFlow task도 operator이므로 `retries`, `retry_delay`, `execution_timeout` 같은
operator argument를 사용할 수 있습니다.

```python
from datetime import timedelta


@task(
    retries=3,
    retry_delay=timedelta(minutes=5),
    execution_timeout=timedelta(minutes=20),
)
def call_external_api() -> dict:
    return {"status": "ok"}
```

공통 설정이 많으면 `@dag(default_args=...)`에 두고, task별로 필요한 값만 override합니다.

```python
from datetime import datetime, timedelta

from airflow.sdk import dag, task


DEFAULT_ARGS = {
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


@dag(
    dag_id="example_taskflow_default_args",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    default_args=DEFAULT_ARGS,
)
def example_taskflow_default_args():
    @task
    def normal_task() -> None:
        print("uses default retries")

    @task(retries=0)
    def no_retry_task() -> None:
        print("override retries")

    normal_task() >> no_retry_task()


example_taskflow_default_args()
```

## Branch와 함께 쓰기

runtime 값에 따라 다른 task path를 실행하려면 `@task.branch`를 사용합니다. branch task는
실행할 downstream `task_id`를 반환합니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_taskflow_branch",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_taskflow_branch():
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
        print("no data")

    @task
    def load_data() -> None:
        print("load data")

    summary = inspect()
    route = choose(summary)
    route >> [skip_load(), load_data()]


example_taskflow_branch()
```

branch에서 선택되지 않은 task는 `skipped`가 됩니다. branch 후 join task가 필요하면
`trigger_rule="none_failed_min_one_success"`를 검토합니다.

## Short-circuit과 함께 쓰기

조건이 "A 또는 B"가 아니라 "계속 실행할지 말지"라면 `@task.short_circuit`이 더 단순합니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_taskflow_short_circuit",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_taskflow_short_circuit():
    @task
    def inspect() -> dict:
        return {"row_count": 10}

    @task.short_circuit
    def has_rows(summary: dict) -> bool:
        return summary["row_count"] > 0

    @task
    def load_data(summary: dict) -> None:
        print(summary)

    summary = inspect()
    gate = has_rows(summary)
    gate >> load_data(summary)


example_taskflow_short_circuit()
```

`has_rows()`가 `False`를 반환하면 downstream task는 `skipped`가 됩니다.

## Dynamic task mapping과 함께 쓰기

입력 개수가 runtime에 결정되면 `.expand()`를 사용합니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_taskflow_mapping",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_taskflow_mapping():
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


example_taskflow_mapping()
```

Airflow scheduler는 `list_files()` 결과 list를 보고 `process_file` task instance를 파일
개수만큼 만듭니다.

## classic operator와 섞어 쓰기

TaskFlow를 쓴다고 모든 task가 `@task`여야 하는 것은 아닙니다. Bash, Email, Kubernetes,
sensor처럼 operator가 더 자연스러운 작업은 classic operator를 그대로 섞어 씁니다.

```python
from datetime import datetime

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import dag, task


@dag(
    dag_id="example_taskflow_mixed",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_taskflow_mixed():
    start = BashOperator(
        task_id="announce_start",
        bash_command='echo "start"',
    )

    @task
    def make_summary() -> dict:
        return {"ok": 1}

    @task
    def report(summary: dict) -> None:
        print(summary)

    summary = make_summary()
    start >> summary
    report(summary)


example_taskflow_mixed()
```

이 repo에도 `airflow_mgmt/dag_templates/mixed_styles_template.py`가 같은 목적의 학습용
template로 있습니다.

## 같은 task 함수를 재사용하기

같은 decorated task를 여러 번 호출해야 하면 `override()`로 `task_id`를 바꿉니다.

```python
@task
def add_one(value: int) -> int:
    return value + 1


first = add_one.override(task_id="add_one_first")(1)
second = add_one.override(task_id="add_one_second")(first)
```

같은 DAG 안에서 동일한 task id가 두 번 생기면 안 됩니다. 재사용 시에는 `task_id` 충돌을
명확히 피합니다.

## 다른 dependency가 필요한 task

task 하나만 worker에 없는 package가 필요하면 `@task.virtualenv`를 사용할 수 있습니다.
이 방식은 task 실행 시 별도 virtualenv를 만들고 requirements를 설치합니다.

```python
@task.virtualenv(
    requirements=["requests==2.32.3"],
    system_site_packages=False,
)
def call_with_isolated_dependency() -> dict:
    import requests

    response = requests.get("https://example.com", timeout=10)
    return {"status_code": response.status_code}
```

주의할 점은 import를 함수 안에 둬야 한다는 것입니다. DAG parse 시점에는 virtualenv가
아직 없으므로 module-level import가 실패할 수 있습니다.

이 repo에는 `airflow_mgmt/dag_templates/virtualenv_task_template.py`가 더 자세한
학습용 template로 있습니다.

## 이 repo에서 TaskFlow DAG를 만들 때

새 업무 DAG를 만들 때는 다음 순서를 권장합니다.

1. `airflow_mgmt/dag_templates/taskflow_decorator_template.py`를 참고합니다.
2. 실제 배포할 DAG는 `airflow_mgmt/dags/<topic>/<name>_dag.py` 아래에 둡니다.
3. DAG 파일에는 `@dag`, `@task`, schedule, dependency wiring만 남깁니다.
4. 긴 업무 로직은 `minio_handler/`, 추후 `utils/`, 또는 import 가능한 helper로 분리합니다.
5. repo-local import가 필요하면 `docs/sys_path_bootstrap.md`의 bootstrap stub 규칙을 따릅니다.
6. 큰 데이터는 XCom에 넣지 않고 MinIO/S3, DB, 파일 경로, object key로 넘깁니다.
7. 변경 후에는 `airflow_mgmt/tests/test_dag_integrity.py`로 DAG import를 검증합니다.

## 흔한 실수

| 실수 | 문제 | 해결 |
|---|---|---|
| TaskFlow 함수를 일반 Python 함수처럼 생각 | DAG parse 시점과 task 실행 시점이 섞임 | 함수 호출 결과는 `XComArg`라고 이해합니다. |
| XComArg 값을 `if`로 직접 판단 | runtime 값이 아직 없음 | `@task.branch` 또는 `@task.short_circuit`를 씁니다. |
| 큰 DataFrame을 return | metadata DB와 UI가 무거워짐 | object storage에 저장하고 key만 return합니다. |
| 같은 task 함수를 여러 번 호출하며 task id 충돌 | DAG parse error 또는 의도치 않은 graph | `.override(task_id=...)`를 사용합니다. |
| branch 후 join이 skip됨 | 기본 trigger rule이 `all_success` | join에는 `none_failed_min_one_success`를 검토합니다. |
| `@task.virtualenv` 함수 밖에서 dependency import | parse 시점에 package가 없어 실패 | import를 virtualenv task 함수 안으로 옮깁니다. |

## 언제 TaskFlow를 쓰고 언제 operator를 쓸까

| 상황 | 권장 |
|---|---|
| Python 함수로 처리하는 업무 로직 | TaskFlow `@task` |
| Python task 간 작은 결과 전달 | TaskFlow return value |
| shell command 실행 | `BashOperator` |
| email 발송 | `EmailOperator` |
| Kubernetes pod 실행 | `KubernetesPodOperator` |
| 외부 event 대기 | sensor 또는 deferrable sensor |
| Python package isolation 필요 | `@task.virtualenv` |

TaskFlow는 classic operator를 없애는 기능이 아니라, Python-heavy DAG를 더 자연스럽게
쓰는 API입니다. operator가 이미 명확한 역할을 제공하는 경우에는 operator를 쓰고,
그 사이의 Python glue나 business logic은 TaskFlow로 작성하면 됩니다.

## 참고 공식 문서

- TaskFlow tutorial: <https://airflow.apache.org/docs/apache-airflow/stable/tutorial/taskflow.html>
- Airflow 3 public interface: <https://airflow.apache.org/docs/apache-airflow/stable/public-airflow-interface.html>
- PythonOperator와 branch decorator: <https://airflow.apache.org/docs/apache-airflow-providers-standard/stable/operators/python.html>

# Airflow로 할 수 있는 일

이 문서는 현재 저장소의 `airflow_mgmt/` 기준으로 Airflow를 어떻게 바라보면
좋은지 정리합니다. 이 repo는 회사 Airflow 3.1.8 플랫폼을 전제로 하며, 실제 등록
DAG는 `airflow_mgmt/dags/`에만 둡니다. `airflow_mgmt/dag_templates/`는 학습용
boilerplate입니다.

## Airflow를 한 문장으로 이해하기

Airflow는 Python 코드로 workflow를 정의하고, 그 workflow를 정해진 시간이나
수동 trigger에 맞춰 실행, 재시도, 분기, 모니터링하는 orchestration 도구입니다.

Airflow가 직접 "데이터 처리 엔진"이 되는 것은 아닙니다. 실제 처리는 Python 함수,
shell command, Kubernetes pod, DB query, HTTP call, MinIO/S3 작업 같은 task가
수행하고, Airflow는 그 task들의 실행 순서와 상태를 관리합니다.

## 대표적으로 할 수 있는 일

| 하고 싶은 일 | Airflow에서 쓰는 기능 | 예시 |
|---|---|---|
| 매일/매시간 작업 실행 | `schedule`, `catchup`, manual trigger | 매일 새벽 FTP log 수집 |
| 작업 순서 정의 | task dependency, `>>`, TaskFlow return passing | `extract >> transform >> load` |
| 실패 시 자동 재시도 | `retries`, `retry_delay`, `execution_timeout` | API timeout이면 3번 재시도 |
| 실패 알림 | `email_on_failure`, `on_failure_callback`, `EmailOperator` | task 실패 시 운영자에게 메일 |
| 조건별 분기 | `@task.branch`, `BranchPythonOperator` | row 수가 0이면 upload skip |
| 조건이 false면 중단 | `@task.short_circuit`, `ShortCircuitOperator` | 데이터가 없으면 downstream skip |
| 실패하면 다른 작업 실행 | `trigger_rule="one_failed"` 등 | 실패 시 rollback, ticket 생성 |
| 항상 cleanup 실행 | `trigger_rule="all_done"` | 임시 파일 삭제 |
| task 간 작은 값 전달 | XCom, TaskFlow return value | `{ok: 10, ng: 2}` summary 전달 |
| 입력 개수만큼 task 복제 | dynamic task mapping, `.expand()` | 파일 목록마다 같은 처리 task 실행 |
| 외부 시스템 대기 | sensor, deferrable sensor | 파일 도착, API 상태, partition 생성 대기 |
| 외부 시스템 연결 | provider operator/hook, Connections | SMTP, Kubernetes, HTTP, DB, S3 |
| 실행 이력 확인 | Airflow UI, task logs, DAG graph | 어떤 task가 왜 실패했는지 확인 |

## Airflow가 특히 잘 맞는 경우

Airflow는 "정해진 순서가 있고, 실패/재시도/관찰이 중요한 batch workflow"에 잘
맞습니다.

좋은 후보는 다음과 같습니다.

- 파일 수집 후 MinIO/S3 업로드
- DB에서 데이터를 읽어 변환 후 다른 저장소에 적재
- 여러 API를 순서대로 호출하고 결과를 검증
- 매일 같은 진단 job을 돌리고 실패 시 알림
- 여러 장비나 여러 파일에 같은 작업을 병렬로 적용
- 실패하면 cleanup이나 보상 작업을 실행해야 하는 pipeline

## Airflow가 애매한 경우

다음은 Airflow만으로 해결하려고 하면 구조가 무거워질 수 있습니다.

| 상황 | 더 맞는 도구 |
|---|---|
| millisecond 단위 실시간 처리 | streaming engine, queue consumer |
| 사용자의 HTTP 요청에 즉시 응답해야 함 | Flask/FastAPI route |
| 장시간 상시 실행 daemon | service, worker process |
| 큰 DataFrame 자체를 task 사이에 전달 | object storage, DB, parquet file |
| DAG 실행 중 Python 코드를 새로 생성하고 배포 | 별도 code generation/deployment flow |

Airflow DAG는 "workflow 정의"입니다. runtime 값에 따라 어떤 task를 실행할지는
정할 수 있지만, 실행 중에 repo의 Python 코드를 새로 작성해서 배포하는 방식으로
쓰는 도구는 아닙니다.

## 이 repo에서 시작할 위치

현재 `airflow_mgmt/`에는 실제 코드와 학습용 코드가 섞여 있습니다.

| 위치 | 의미 |
|---|---|
| `dags/` | 회사 Airflow가 실제로 읽고 실행하는 DAG |
| `dags/diagnostics/inspect_packages_dag.py` | worker 설치 package를 로그로 출력하는 실제 진단 DAG |
| `dag_templates/taskflow_decorator_template.py` | 일반 업무 DAG의 기본 template. `@dag`, `@task`, repo-local helper import, 필요한 classic operator 혼합을 포함합니다. |
| `dag_templates/with_dag_template.py` | `with DAG(...)`, `PythonOperator`, manual XCom pull을 명시적으로 쓰는 classic template |
| `dag_templates/virtualenv_task_template.py` | worker에 없는 package가 필요한 task용 isolated virtualenv template |
| `docs/keeping_dags_thin.md` | DAG 파일을 얇게 유지하는 repo-local 원칙 |

새 업무 DAG를 만들 때는 먼저 `taskflow_decorator_template.py`를 복사해
`dags/<topic>/<name>_dag.py`로 옮깁니다. operator 객체와 XCom pull을 명시적으로 보고
싶으면 `with_dag_template.py`를 사용합니다. 특정 task에 worker에 없는 package가 필요할
때만 `virtualenv_task_template.py`를 사용합니다. 긴 업무 로직은 `minio_handler/`나 추후
`utils/` 같은 repo-local helper로 분리합니다.

## 작은 예시

아래는 "목록을 만들고, 항목마다 처리하고, 마지막에 summary를 만드는" 전형적인
Airflow 흐름입니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_daily_ingest",
    start_date=datetime(2026, 1, 1),
    schedule="@daily",
    catchup=False,
    tags=["example"],
)
def example_daily_ingest():
    @task
    def list_objects() -> list[str]:
        return ["raw/a.log", "raw/b.log"]

    @task
    def process_object(object_key: str) -> dict:
        return {"object_key": object_key, "rows": 100}

    @task
    def summarize(results: list[dict]) -> None:
        total_rows = sum(item["rows"] for item in results)
        print(f"total_rows={total_rows}")

    objects = list_objects()
    results = process_object.expand(object_key=objects)
    summarize(results)


example_daily_ingest()
```

여기서 `list_objects()`의 return value는 XCom으로 저장되고, scheduler는 그 값을
보고 `process_object` task instance를 object 개수만큼 runtime에 확장합니다.

## 학습 순서

처음에는 다음 순서로 익히는 것이 좋습니다.

1. `@dag`, `@task`, `>>`로 기본 task graph를 만듭니다.
2. task return value가 XCom으로 downstream에 전달되는 흐름을 익힙니다.
3. `@task.branch`와 `@task.short_circuit`로 조건부 실행을 만듭니다.
4. `trigger_rule`로 실패 시 알림, rollback, cleanup task를 붙입니다.
5. `.expand()`로 입력 개수만큼 병렬 task를 만드는 dynamic task mapping을 사용합니다.
6. 필요할 때 provider operator/hook로 SMTP, Kubernetes, HTTP, DB, object storage에 연결합니다.

## 참고 공식 문서

- Airflow 소개: <https://airflow.apache.org/docs/apache-airflow/3.1.5/index.html>
- Airflow 공개 인터페이스: <https://airflow.apache.org/docs/apache-airflow/stable/public-airflow-interface.html>
- Dynamic task mapping 가이드: <https://airflow.apache.org/docs/apache-airflow/3.1.5/authoring-and-scheduling/dynamic-task-mapping.html>

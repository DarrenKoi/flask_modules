# Airflow 실패 알림과 실패 대응 패턴

Airflow에서 "실패하면 뭔가 한다"는 요구는 세 가지로 나눠서 설계하는 것이 좋습니다.

| 목적 | 권장 기능 | 넣을 로직 |
|---|---|---|
| 사람에게 알림 | `email_on_failure`, `on_failure_callback`, `EmailOperator` | 메일, Slack, Teams, ticket 생성 |
| workflow 보상 작업 | `trigger_rule="one_failed"` 또는 `"all_failed"` | rollback, quarantine, fail marker 생성 |
| 자원 정리 | `trigger_rule="all_done"` | 임시 파일 삭제, lock 해제, scratch 정리 |

callback은 알림처럼 짧은 side effect에 적합합니다. 실제 업무 보상 작업은 task로
표현해야 Airflow UI에서 상태, 로그, 재시도, dependency를 볼 수 있습니다.

## 실패 메일을 보내는 방법

메일 전송은 회사 Airflow 플랫폼에 SMTP 설정 또는 SMTP provider가 준비되어 있어야
동작합니다. Airflow 3 계열에서는 provider 방식의
`airflow.providers.smtp` package와 `smtp_default` connection을 확인하는 것이
가장 명확합니다.

### 1. 기본 실패 메일

가장 단순한 형태입니다. platform SMTP가 이미 설정되어 있을 때 task 실패마다 메일을
보냅니다.

```python
from datetime import datetime, timedelta

from airflow.sdk import dag, task


DEFAULT_ARGS = {
    "email": ["ops@example.com"],
    "email_on_failure": True,
    "email_on_retry": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
}


@dag(
    dag_id="example_email_on_failure",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    default_args=DEFAULT_ARGS,
)
def example_email_on_failure():
    @task
    def may_fail() -> None:
        raise RuntimeError("sample failure")

    may_fail()


example_email_on_failure()
```

장점은 설정이 작다는 점입니다. 단점은 메일 제목과 본문을 세밀하게 제어하기 어렵고,
회사 플랫폼 SMTP 설정에 강하게 의존한다는 점입니다.

### 2. `on_failure_callback`으로 직접 알림 작성

callback은 task나 DAG에 붙일 수 있습니다. 실패한 DAG, task, run id, log URL 같은
context를 읽어 메일 본문을 만들 수 있습니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


def notify_failure(context: dict) -> None:
    from airflow.providers.smtp.hooks.smtp import SmtpHook

    dag_id = context["dag"].dag_id
    ti = context["task_instance"]
    run_id = context["run_id"]
    log_url = getattr(ti, "log_url", "")

    SmtpHook(smtp_conn_id="smtp_default").send_email_smtp(
        to=["ops@example.com"],
        subject=f"[Airflow 실패] {dag_id}.{ti.task_id}",
        html_content=f"""
        <h3>Airflow task failed</h3>
        <ul>
          <li>DAG: {dag_id}</li>
          <li>Task: {ti.task_id}</li>
          <li>Run: {run_id}</li>
          <li>Log: <a href="{log_url}">{log_url}</a></li>
        </ul>
        """,
    )


@dag(
    dag_id="example_failure_callback",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    on_failure_callback=notify_failure,
)
def example_failure_callback():
    @task
    def run_job() -> None:
        raise RuntimeError("failed")

    run_job()


example_failure_callback()
```

callback 안에는 오래 걸리는 업무 로직을 넣지 않습니다. callback 실패도 별도 task처럼
retry 관리되는 것이 아니므로, 알림 payload를 만들고 외부 알림 시스템에 보내는 정도로
제한합니다.

### 3. 실패 알림 자체를 task로 만들기

메일 발송도 DAG graph의 일부로 보고 싶다면 `EmailOperator`를 downstream task로 둡니다.
이 방식은 Airflow UI에서 알림 task의 성공/실패를 볼 수 있다는 장점이 있습니다.

```python
from datetime import datetime

from airflow.providers.smtp.operators.smtp import EmailOperator
from airflow.sdk import dag, task


@dag(
    dag_id="example_email_operator_on_failure_path",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_email_operator_on_failure_path():
    @task
    def extract() -> dict:
        return {"rows": 10}

    @task
    def load(summary: dict) -> None:
        raise RuntimeError(f"load failed: {summary}")

    source = extract()
    loaded = load(source)

    notify_failed = EmailOperator(
        task_id="notify_failed",
        to=["ops@example.com"],
        subject="[Airflow 실패] example_email_operator_on_failure_path",
        html_content="하나 이상의 upstream task가 실패했습니다.",
        trigger_rule="one_failed",
    )

    [source, loaded] >> notify_failed


example_email_operator_on_failure_path()
```

`trigger_rule="one_failed"`는 upstream 중 하나라도 실패하면 실행됩니다. 다만 모든
실패 상황에서 같은 알림을 보내도 되는지, 실패한 task별로 다른 본문이 필요한지는
DAG마다 결정해야 합니다.

## `trigger_rule`로 실패 후 작업 실행하기

기본값은 `all_success`입니다. 즉 upstream이 모두 성공해야 downstream이 실행됩니다.
실패 대응 task를 만들려면 의도에 맞는 `trigger_rule`을 지정해야 합니다.

| `trigger_rule` 값 | 의미 | 자주 쓰는 용도 |
|---|---|---|
| `all_success` | 모든 upstream 성공 시 실행 | 정상 처리 경로 |
| `one_failed` | upstream 중 하나 이상 실패 시 실행 | 실패 알림, rollback 시작 |
| `all_failed` | 모든 upstream이 실패 또는 upstream_failed일 때 실행 | 모든 대안 실패 후 대체 처리 |
| `all_done` | 성공/실패/skipped와 관계없이 upstream 종료 후 실행 | cleanup, lock 해제 |
| `none_failed` | 실패가 없으면 실행, skipped는 허용 | branch 이후 join |
| `none_failed_min_one_success` | 실패가 없고 하나 이상 성공하면 실행 | branch 이후 정상 join |

branch 뒤에서 join task를 만들 때는 기본값 `all_success` 때문에 join이 skip될 수
있습니다. branch로 선택되지 않은 path는 `skipped`가 되므로, join에는 보통
`none_failed` 또는 `none_failed_min_one_success`를 씁니다.

## 실패하면 rollback, 항상 cleanup

다음 예시는 load가 실패하면 rollback을 실행하고, 성공/실패와 관계없이 cleanup을
실행하는 구조입니다.

```python
from datetime import datetime

from airflow.sdk import dag, task


@dag(
    dag_id="example_failure_recovery",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
)
def example_failure_recovery():
    @task
    def reserve_target() -> dict:
        return {"target_table": "daily_orders", "reservation_id": "abc123"}

    @task
    def load_data(reservation: dict) -> None:
        raise RuntimeError(f"load failed for {reservation['target_table']}")

    @task(trigger_rule="one_failed")
    def rollback() -> None:
        print("rollback partial writes or mark batch as failed")

    @task(trigger_rule="all_done")
    def cleanup() -> None:
        print("remove temp files and release locks")

    reserved = reserve_target()
    loaded = load_data(reserved)
    rollback_task = rollback()
    cleanup_task = cleanup()

    [reserved, loaded] >> rollback_task
    [reserved, loaded, rollback_task] >> cleanup_task


example_failure_recovery()
```

실무에서는 rollback task가 upstream XCom에 의존하지 않도록 설계하는 편이 안전합니다.
upstream task가 실패하면 return value XCom이 없을 수 있기 때문입니다. rollback에
필요한 batch id, target table, object key는 deterministic하게 만들거나 Airflow
`run_id`에서 계산할 수 있게 두는 것이 좋습니다.

## retry와 알림의 관계

`retries`가 설정되어 있으면 task는 실패 직후 바로 최종 실패가 아닐 수 있습니다.
보통 운영 알림은 "모든 retry가 끝난 최종 실패"에 보내는 편이 덜 시끄럽습니다.

| 상황 | 권장 |
|---|---|
| 일시적 network 오류가 흔함 | `retries`와 `retry_delay`를 먼저 설정 |
| retry 중에도 경고가 필요함 | `on_retry_callback` 사용 |
| 최종 실패만 알리면 됨 | `on_failure_callback` 또는 `email_on_failure` 사용 |
| 실패 즉시 별도 workflow를 시작해야 함 | `trigger_rule="one_failed"` task 사용 |

## 참고 공식 문서

- Callback 종류: <https://airflow.staged.apache.org/docs/apache-airflow/stable/administration-and-deployment/logging-monitoring/callbacks.html>
- Trigger rule 설명: <https://airflow.apache.org/docs/apache-airflow/3.1.5/core-concepts/dags.html#trigger-rules>
- SMTP EmailOperator 설명: <https://airflow.apache.org/docs/apache-airflow-providers-smtp/stable/_api/airflow/providers/smtp/operators/smtp/index.html>
- SMTP connection 설정: <https://airflow.apache.org/docs/apache-airflow-providers-smtp/stable/connections/smtp.html>

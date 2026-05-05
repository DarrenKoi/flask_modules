# Airflow API로 DAG 실행하기

이 문서는 회사 Airflow 3.1.8 환경에서 API로 작업을 실행하거나 재실행할 때의
기본 절차를 정리합니다. 현재 이 repo에서 실제 등록되는 DAG는
`airflow_mgmt/dags/diagnostics/inspect_packages_dag.py` 하나이며, DAG ID는
`diagnostics_inspect_packages`, task ID는 `list_packages`입니다.

## 결론

Airflow에는 API로 실행을 요청하는 방법이 있습니다. 다만 Airflow의 기본 실행 단위는
독립 Python 함수가 아니라 **DAG run 안의 task instance**입니다.

| 하고 싶은 일 | 권장 방법 |
|---|---|
| 수동/API로 workflow 한 번 실행 | `POST /api/v2/dags/{dag_id}/dagRuns` |
| 기존 DAG run의 특정 task를 다시 실행 | `POST /api/v2/dags/{dag_id}/clearTaskInstances` |
| 기존 DAG run 전체를 다시 실행 | `POST /api/v2/dags/{dag_id}/dagRuns/{dag_run_id}/clear` |
| API에서 하나의 업무만 실행 | `schedule=None`인 작은 DAG로 만들고 DAG run을 trigger |

즉 "task 하나를 API로 바로 호출"하는 방식보다는, task가 들어 있는 DAG run을 만들거나
기존 task instance를 clear해서 scheduler/executor가 다시 실행하게 하는 방식으로 봅니다.

## 먼저 확인할 것

회사 Airflow가 관리형 플랫폼이면 API 인증 방식과 권한은 플랫폼 설정을 따릅니다.
다음 값을 먼저 확인합니다.

| 값 | 설명 |
|---|---|
| `AIRFLOW_URL` | Airflow API server URL. 예: `https://airflow.example.com` |
| API 계정 또는 token | DAG run 생성 권한이 있는 계정 |
| DAG ID | 현재 repo 예시는 `diagnostics_inspect_packages` |
| DAG pause 상태 | pause된 DAG는 run이 생성되어도 실제 task가 돌지 않을 수 있음 |

Airflow 3 public API는 보통 `/api/v2` 아래에 있습니다. 인증은 환경에 따라 JWT,
Basic auth, SSO proxy, 사내 gateway token 등으로 달라질 수 있으므로 회사 플랫폼
가이드를 우선합니다.

## PowerShell 예시

아래 예시는 Airflow의 `/auth/token` 방식이 열려 있는 환경을 가정합니다. 회사 SSO나
gateway가 앞에 있으면 token 발급 부분만 사내 방식으로 바꾸고, DAG trigger 요청의
URL과 JSON body는 같은 흐름으로 사용합니다.

```powershell
$env:AIRFLOW_URL = "https://airflow.example.com"
$env:AIRFLOW_USER = "your-user"
$env:AIRFLOW_PASSWORD = "your-password"

$tokenBody = @{
    username = $env:AIRFLOW_USER
    password = $env:AIRFLOW_PASSWORD
} | ConvertTo-Json

$tokenResponse = Invoke-RestMethod `
    -Method Post `
    -Uri "$env:AIRFLOW_URL/auth/token" `
    -ContentType "application/json" `
    -Body $tokenBody

$env:AIRFLOW_TOKEN = $tokenResponse.access_token
```

현재 repo의 진단 DAG를 API로 실행합니다.

```powershell
$dagId = "diagnostics_inspect_packages"
$runId = "manual_api_$(Get-Date -Format 'yyyyMMdd_HHmmss')"

$runBody = @{
    dag_run_id = $runId
    conf = @{
        reason = "api smoke test"
    }
    note = "Triggered from API"
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
    -Method Post `
    -Uri "$env:AIRFLOW_URL/api/v2/dags/$dagId/dagRuns" `
    -Headers @{
        Authorization = "Bearer $env:AIRFLOW_TOKEN"
        Accept = "application/json"
    } `
    -ContentType "application/json" `
    -Body $runBody
```

상태를 확인합니다.

```powershell
Invoke-RestMethod `
    -Method Get `
    -Uri "$env:AIRFLOW_URL/api/v2/dags/$dagId/dagRuns/$runId" `
    -Headers @{
        Authorization = "Bearer $env:AIRFLOW_TOKEN"
        Accept = "application/json"
    }
```

`list_packages` task instance 상태를 확인합니다.

```powershell
Invoke-RestMethod `
    -Method Get `
    -Uri "$env:AIRFLOW_URL/api/v2/dags/$dagId/dagRuns/$runId/taskInstances/list_packages" `
    -Headers @{
        Authorization = "Bearer $env:AIRFLOW_TOKEN"
        Accept = "application/json"
    }
```

task log는 try number를 붙여 조회합니다. 첫 실행은 보통 `1`입니다.

```powershell
Invoke-RestMethod `
    -Method Get `
    -Uri "$env:AIRFLOW_URL/api/v2/dags/$dagId/dagRuns/$runId/taskInstances/list_packages/logs/1" `
    -Headers @{
        Authorization = "Bearer $env:AIRFLOW_TOKEN"
        Accept = "application/json"
    }
```

## curl 예시

Linux, macOS, WSL, 또는 Windows의 `curl.exe`에서 같은 요청을 보낼 수 있습니다.

```bash
export AIRFLOW_URL="https://airflow.example.com"
export AIRFLOW_TOKEN="replace-with-token"

curl -sS -X POST "$AIRFLOW_URL/api/v2/dags/diagnostics_inspect_packages/dagRuns" \
  -H "Authorization: Bearer $AIRFLOW_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{
    "dag_run_id": "manual_api_20260505_130000",
    "conf": {
      "reason": "api smoke test"
    },
    "note": "Triggered from API"
  }'
```

`dag_run_id`는 같은 DAG 안에서 중복되면 안 됩니다. 자동 생성을 원하면 body에서
`dag_run_id`를 빼도 됩니다. 운영 자동화에서는 조회와 재시도를 쉽게 하려고 의미 있는
run ID를 직접 넣는 편이 좋습니다.

## 특정 task만 다시 실행하기

이미 만들어진 DAG run 안의 특정 task를 다시 돌리고 싶다면 task instance를 clear합니다.
기본값이 dry run인 항목이 있으므로 실제 재실행을 원할 때는 `dry_run=false`를 명시합니다.

```powershell
$clearBody = @{
    dag_run_id = $runId
    task_ids = @("list_packages")
    dry_run = $false
    only_failed = $false
    include_downstream = $false
    include_upstream = $false
    reset_dag_runs = $true
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
    -Method Post `
    -Uri "$env:AIRFLOW_URL/api/v2/dags/$dagId/clearTaskInstances" `
    -Headers @{
        Authorization = "Bearer $env:AIRFLOW_TOKEN"
        Accept = "application/json"
    } `
    -ContentType "application/json" `
    -Body $clearBody
```

이 요청은 task 함수를 API server 프로세스에서 직접 실행하는 것이 아닙니다. metadata DB의
task instance 상태를 clear하고, scheduler/executor가 다시 실행 대상으로 보게 만듭니다.

전체 DAG run을 다시 실행하려면 다음 endpoint를 사용합니다.

```powershell
$clearRunBody = @{
    dry_run = $false
    only_failed = $false
} | ConvertTo-Json

Invoke-RestMethod `
    -Method Post `
    -Uri "$env:AIRFLOW_URL/api/v2/dags/$dagId/dagRuns/$runId/clear" `
    -Headers @{
        Authorization = "Bearer $env:AIRFLOW_TOKEN"
        Accept = "application/json"
    } `
    -ContentType "application/json" `
    -Body $clearRunBody
```

## API로 실행할 업무 DAG 설계

외부 시스템에서 "필요할 때만 실행"하는 업무라면 DAG를 다음처럼 설계합니다.

```python
from datetime import datetime

from airflow.sdk import dag, get_current_context, task


@dag(
    dag_id="orders_manual_sync",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    tags=["orders"],
)
def orders_manual_sync():
    @task
    def run_sync() -> None:
        context = get_current_context()
        conf = context["dag_run"].conf or {}
        target_date = conf.get("target_date")
        print(f"target_date={target_date}")

    run_sync()


orders_manual_sync()
```

그 뒤 API body의 `conf`로 실행 파라미터를 넘깁니다.

```json
{
  "dag_run_id": "manual_api_orders_20260505_130000",
  "conf": {
    "target_date": "2026-05-05"
  }
}
```

runtime에 Python 코드를 새로 만들어 실행하는 방식은 피합니다. 배포되는 코드는
`airflow_mgmt/dags/`와 repo-local helper에 두고, API는 "어떤 DAG run을 어떤 입력으로
만들지"만 결정하게 둡니다.

## 자주 나는 오류

| 증상 | 볼 것 |
|---|---|
| `401` | token 없음, 만료, 인증 backend 불일치 |
| `403` | 계정에 DAG run 생성 또는 task clear 권한 없음 |
| `404` | DAG ID 오타, DAG import 실패, 아직 dag-processor가 새 파일을 parse하지 않음 |
| `409` | 같은 `dag_run_id`가 이미 있음 |
| run은 생겼지만 task가 안 뜀 | DAG가 pause 상태인지, worker slot/pool이 꽉 찼는지 확인 |
| task만 API로 바로 실행하고 싶음 | 작은 `schedule=None` DAG로 만들거나 기존 task instance clear 사용 |

## 참고 공식 문서

- Airflow 3.1.8 REST API reference: <https://airflow.apache.org/docs/apache-airflow/3.1.8/stable-rest-api-ref.html>
- Airflow DAG run external trigger 설명: <https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/dag-run.html#external-triggers>
- Apache Airflow Python API client endpoint 목록: <https://github.com/apache/airflow-client-python>

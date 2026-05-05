# Windows에서 Airflow DAG를 로컬 검증하기

Airflow scheduler는 POSIX fork 동작에 의존하므로 Windows Python에서 네이티브로
운영용 scheduler를 돌리는 방식은 권장되지 않습니다. 이 repo에서는 두 단계로
로컬 확인을 합니다.

1. Windows Python에서 DAG import와 helper unit test를 빠르게 검증합니다.
2. scheduler 동작까지 봐야 할 때만 WSL2에서 `airflow standalone`을 실행합니다.

현재 등록된 DAG는 `diagnostics_inspect_packages` 하나입니다. 이 DAG는 수동 실행용
진단 DAG이며, Airflow worker에 설치된 Python 패키지 목록을 `list_packages` task
로그에 출력합니다.

## Option 1: Windows Python으로 빠른 검증

가장 먼저 쓸 기본 루프입니다. Airflow UI나 scheduler를 띄우지 않고 import error,
syntax error, DAG cycle, helper unit test를 잡습니다.

```powershell
cd C:\Code\flask_modules\airflow_mgmt

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --constraint "https://raw.githubusercontent.com/apache/airflow/constraints-3.1.8/constraints-3.11.txt"

$env:AIRFLOW_MGMT_ROOT = (Get-Location).Path
$env:AIRFLOW_MGMT_SCRATCH_ROOT = Join-Path (Get-Location).Path "scratch"

.\.venv\Scripts\python.exe -m pytest tests -v
```

PowerShell 실행 정책 때문에 `Activate.ps1`이 막히는 환경이 있을 수 있습니다.
그 경우에도 위처럼 `.\.venv\Scripts\python.exe`를 직접 호출하면 됩니다.

`tests/test_dag_integrity.py`는 Airflow scheduler 없이 `dags/`를 DagBag으로 parse합니다.
정상 상태라면 현재 DAG 목록에 `diagnostics_inspect_packages`가 보여야 합니다.

## Option 2: WSL2에서 `airflow standalone` 실행

scheduler, dag-processor, API server, SQLite metadata DB까지 포함한 단일 프로세스
Airflow를 띄워서 UI와 task log를 확인할 때 사용합니다.

PowerShell 관리자 창에서 WSL을 설치합니다.

```powershell
wsl --install -d Ubuntu
```

재부팅 후 Ubuntu shell에서 Python을 준비합니다.

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip
```

각 개발 세션에서는 다음처럼 실행합니다.

```bash
cd /mnt/c/Code/flask_modules/airflow_mgmt

python3.11 -m venv .venv
source .venv/bin/activate

CONSTRAINT="https://raw.githubusercontent.com/apache/airflow/constraints-3.1.8/constraints-3.11.txt"
pip install -r requirements.txt --constraint "$CONSTRAINT"

export AIRFLOW_HOME="$(pwd)/.airflow"
export AIRFLOW__CORE__DAGS_FOLDER="$(pwd)/dags"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW_MGMT_ROOT="$(pwd)"
export AIRFLOW_MGMT_SCRATCH_ROOT="$(pwd)/scratch"

airflow standalone
```

콘솔에 자동 생성된 admin password가 출력됩니다. UI는 보통
`http://localhost:8080`에서 열립니다.

## 현재 DAG 실행 확인

WSL에서 `airflow standalone`을 띄운 뒤 다른 WSL shell에서 같은 venv와 환경 변수를
설정하고 다음 명령을 실행합니다.

```bash
airflow dags list | grep diagnostics_inspect_packages
airflow dags trigger diagnostics_inspect_packages
airflow dags test diagnostics_inspect_packages 2026-01-01
airflow tasks test diagnostics_inspect_packages list_packages 2026-01-01
```

`tasks test`는 scheduler 없이 단일 task를 foreground에서 실행하므로, 패키지 목록
로그를 가장 빠르게 확인할 수 있습니다.

## runtime_paths 환경 변수

로컬과 회사 Airflow 플랫폼에서 같은 import 규칙을 쓰기 위해 다음 값을 맞춥니다.

| 환경 변수 | 값 |
|---|---|
| `AIRFLOW_MGMT_ROOT` | `runtime_paths.py`가 있는 `airflow_mgmt/` 절대 경로 |
| `AIRFLOW_MGMT_SCRATCH_ROOT` | task가 임시 파일을 쓸 writable 폴더 |

이 값은 Airflow UI의 Variables가 아니라 Python 프로세스가 읽는 OS 환경 변수입니다.
`.env` 파일을 repo에 두는 것만으로 Airflow가 자동으로 읽지는 않습니다. 로컬에서는
PowerShell의 `$env:...` 또는 WSL의 `export ...`로 설정하고, 회사 Airflow에서는
scheduler, dag-processor, worker 컨테이너나 서비스의 환경 변수로 주입해야 합니다.

`AIRFLOW_MGMT_ROOT`는 `dags/` 경로가 아닙니다. 예를 들어 Windows에서는
`C:\Code\flask_modules\airflow_mgmt`, WSL에서는 `/mnt/c/Code/flask_modules/airflow_mgmt`
처럼 설정합니다.

`AIRFLOW_MGMT_SCRATCH_ROOT`를 설정하지 않으면 Airflow 런타임에서는 OS temp 아래
`airflow_mgmt`를 사용하고, 일반 로컬 실행에서는 `airflow_mgmt/scratch`를 사용합니다.
운영 task가 파일을 내려받거나 중간 산출물을 만들 때는 git checkout 대신 이 scratch
경로를 사용해야 합니다.

## 언제 무엇을 쓸지

| 상황 | 권장 방법 |
|---|---|
| DAG가 회사 Airflow에서 import될지 확인 | Option 1 |
| helper 함수의 데이터 변환 결과 확인 | Option 1 |
| UI에서 DAG와 task log 확인 | Option 2 |
| schedule, retry, XCom 흐름 확인 | Option 2 |
| worker에 특정 패키지가 설치되어 있는지 확인 | `diagnostics_inspect_packages` 수동 실행 |

기본은 Option 1입니다. `pytest`가 실패하면 운영 Airflow UI에서도 DAG가 보이지 않을
가능성이 높으므로 먼저 그 문제를 고칩니다.

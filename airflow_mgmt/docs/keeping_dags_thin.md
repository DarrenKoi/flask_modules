# DAG를 얇게 유지하기

DAG 파일은 "무엇을 언제 실행하는지"를 보여주는 오케스트레이션 계층으로 유지하고,
실제 Python 로직은 `airflow_mgmt/` 아래의 재사용 가능한 모듈로 분리합니다.

현재 이 저장소의 실제 DAG는 `dags/diagnostics/inspect_packages_dag.py` 하나입니다.
이 DAG는 Airflow worker에 설치된 Python 패키지 목록을 로그로 출력하는 진단용 DAG이며,
repo-local helper를 아직 가져오지 않습니다. 앞으로 업무 DAG를 추가할 때는 이 문서의
패턴을 기준으로 DAG를 얇게 유지합니다.

## 현재 DAG

| DAG 파일 | DAG ID | 스케줄 | 태그 | 역할 |
|---|---|---:|---|---|
| `dags/diagnostics/inspect_packages_dag.py` | `diagnostics_inspect_packages` | 수동 실행 | `diagnostics` | worker에 설치된 Python 패키지와 버전을 task 로그에 기록 |

`diagnostics_inspect_packages`는 패키지 설치 여부를 확인하기 위한 운영 진단 도구입니다.
Airflow UI에서 수동으로 실행한 뒤 `list_packages` task 로그를 보면 됩니다.

## 권장 구조

현재 `airflow_mgmt/`의 재사용 계층은 DAG 폴더 밖에 있습니다.

```text
airflow_mgmt/
├── dags/                             # 회사 Airflow에서 실제로 실행되는 DAG
│   └── diagnostics/
│       └── inspect_packages_dag.py   # 현재 등록된 진단 DAG
├── minio_handler/                    # MinIO / S3 호환 client wrapper
├── utils/                            # 추후 helper 추가 시 사용할 빈 폴더
├── scripts/
│   └── ftp_download_sample.py        # 학습용 참고 코드 (배포 대상 아님)
├── dag_templates/                    # 학습용 DAG 작성 기본 틀 (자동 로드 안 됨)
├── requirements/                     # @task.virtualenv용 task별 pip 요구사항
│   └── probe_task.txt
└── tests/
    ├── conftest.py                   # pytest에서 AIRFLOW_MGMT_ROOT 기본값 설정
    └── test_dag_integrity.py         # DagBag 기반 DAG import 검증
```

`dags/`만 회사 Airflow에 실제로 등록되어 실행됩니다. `dag_templates/`와
`scripts/ftp_download_sample.py`는 아직 Airflow에 익숙하지 않을 때
패턴을 익히기 위해 만든 학습용 코드이며 운영에서 동작하지 않습니다.

업무 DAG를 새로 만들 때는 다음 기준으로 나눕니다.

| 위치 | 넣을 내용 | `airflow` import |
|---|---|---|
| `dags/<topic>/<name>_dag.py` | `@dag`, `@task`, schedule, retry, task wiring | 허용 |
| `minio_handler/*.py` | MinIO / S3 호환 저장소 wrapper | 금지 |
| `utils/*.py` (추후 추가) | 순수 함수, dataclass, 데이터 변환 규칙 | 금지 |
| `tests/test_*.py` | helper 동작과 DAG import 검증 | 보통 금지 |

## repo-local import 규칙

회사 Airflow가 이 repo를 clone하거나 mount하더라도 `airflow_mgmt/` root가 항상
`sys.path`에 들어간다고 가정하면 안 됩니다. 그래서 repo-local helper를 import하는
DAG 파일과 entry-point script는 파일 상단에 작은 **bootstrap stub**을 둡니다.
이 stub이 `airflow_mgmt/` 디렉터리를 찾아 `sys.path`에 넣은 뒤에야 repo-local
import가 가능합니다.

bootstrap stub이 읽는 환경 변수는 하나입니다.

| 환경 변수 | 의미 |
|---|---|
| `AIRFLOW_MGMT_ROOT` | `airflow_mgmt/` 절대 경로 (auto-detect 실패 시 override) |

추가로 `scripts/ftp_download_sample.py`만 사용하는 변수가 하나 더 있습니다.

| 환경 변수 | 의미 |
|---|---|
| `AIRFLOW_MGMT_SCRATCH_ROOT` | task 실행 중 다운로드, 임시 파일 등을 둘 writable scratch 경로 |

두 값 모두 Airflow 내장 설정이나 Airflow UI의 Variables가 아니라 OS 환경 변수입니다.
`os.getenv()`로 읽기 때문에 `.env` 파일을 repo에 만든다고 자동으로 적용되지 않습니다.
로컬에서는 shell에서 직접 설정하고 (`$env:AIRFLOW_MGMT_ROOT="..."` PowerShell,
`export AIRFLOW_MGMT_ROOT=...` bash), 운영 Airflow에서는 scheduler, dag-processor,
worker 프로세스가 뜰 때 해당 환경 변수가 들어가도록 플랫폼 설정에 반영합니다.

`AIRFLOW_MGMT_ROOT`는 `dags/`가 아니라 그 부모인 `airflow_mgmt/`를 가리켜야 합니다.
auto-detect는 `__file__`에서 위로 올라가며 `project_root.txt` 마커 파일을
가진 폴더를 찾는 방식입니다. 폴더 이름이 무엇이든(예: clone 시 `repo/`로 받은 경우)
마커 파일만 있으면 동작하므로, 마커가 사라졌거나 부모 디렉터리에 마커가 없는
경로에서 실행하는 경우에만 env 변수로 명시해 주면 됩니다.

업무 DAG에서 repo-local package를 가져와야 한다면 파일 상단에 다음 stub을 둡니다.

```python
import os, sys
from pathlib import Path

# ── sys.path bootstrap ──────────────────────────────────────────────────────
ROOT_DIR = Path(os.getenv("AIRFLOW_MGMT_ROOT") or next(
    (str(p) for p in Path(__file__).resolve().parents if (p / "project_root.txt").is_file()),
    "",
)).resolve()
if not ROOT_DIR.is_dir():
    raise RuntimeError("Cannot find airflow_mgmt root. Set AIRFLOW_MGMT_ROOT.")
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
# ────────────────────────────────────────────────────────────────────────────

from minio_handler import MinioObject       # noqa: E402
```

이 stub이 필요한 이유는 dag-processor가 각 DAG 파일을 **package의 일부가 아닌
top-level script로 parse**하기 때문입니다. `dags/`만 sys.path에 들어가는
환경에서는 `airflow_mgmt/` root를 직접 추가해야 `minio_handler` 같은 repo-local
package를 top-level 이름으로 import할 수 있습니다.

stub을 entry-point 파일마다 복사하는 것은 의도된 패턴입니다. helper 모듈로
분리하면 그 helper 자체를 import하는 데 다시 순환 문제가 생깁니다.
9줄을 매 파일에 두는 편이 학습 곡선과 배포 양쪽에서 더 단순합니다.

## scratch 경로 규칙

task가 생성하는 다운로드 파일, 변환 중간 결과, 임시 payload는 git checkout 아래에
바로 쓰지 않습니다. Airflow worker에서 git mount는 보통 read-only이기 때문입니다.
`scripts/ftp_download_sample.py`의 `_scratch_root()` helper가 이 분기를 담당합니다.

우선순위는 다음과 같습니다.

1. `AIRFLOW_MGMT_SCRATCH_ROOT`가 있으면 그 경로를 사용합니다.
2. Airflow 런타임으로 보이면 OS temp 아래 `airflow_mgmt`를 사용합니다.
   판단 기준은 `AIRFLOW_HOME`, `AIRFLOW_CTX_DAG_ID`,
   `AIRFLOW__CORE__DAGS_FOLDER`, 또는 cwd의 `/opt/airflow`, `/ops/airflow`입니다.
3. 그 외 로컬 실행에서는 `ROOT_DIR/scratch`를 사용합니다.

다른 script가 동일한 scratch 로직이 필요해지면 `_scratch_root()`를 그쪽에도
복사하거나 공용 helper로 분리합니다. 현재는 사용처가 한 곳뿐이라 inline 상태로
두고 있습니다.

## 얇은 DAG 예시

아래 예시는 같은 DAG 파일 상단에서 위의 `ROOT_HINT` / `bootstrap_sys_path()` 준비 코드가
이미 실행되었다고 가정합니다.

```python
from airflow.sdk import dag, task

@dag(
    dag_id="orders_daily_summary",
    schedule=None,
    catchup=False,
    tags=["orders"],
)
def orders_daily_summary():
    @task
    def summarize(rows: list[dict]) -> dict[str, float | int]:
        # 실제 helper module이 추가되면 여기서 import해서 호출합니다.
        # 예: from utils.orders import daily_summary, parse_orders
        return {"rows": len(rows)}

    summarize([])

orders_daily_summary()
```

DAG 파일에는 task의 경계와 데이터 흐름만 남기고, 검증 규칙과 계산 로직은
`utils/<helper>.py`처럼 Airflow와 분리된 파일에 둡니다 (현재 `utils/`는 빈
폴더입니다).

## 피해야 할 패턴

1. 하나의 `@task` 안에 수백 줄의 업무 로직을 넣는 방식.
2. 직접 관리하는 Python 코드를 `BashOperator("python my_job.py")`로 우회 실행하는 방식.
3. DAG 파일마다 서버 절대 경로를 `sys.path.append("/some/server/path")`로 박는 방식.
4. DAG 파일에서 `from .utils import ...` 같은 상대 import를 쓰는 방식.
5. helper 모듈 (`utils/`, `minio_handler/` 등)이 `airflow.*`를 import하도록 만드는 방식.

## 검증 명령

문서와 코드를 바꾼 뒤에는 `airflow_mgmt/`에서 다음 명령을 실행합니다.

```powershell
python -m pytest tests/test_dag_integrity.py -v
```

`test_dag_integrity.py`는 Airflow scheduler를 띄우지 않고 DagBag으로 DAG import만
검증합니다. import error가 있으면 운영 UI에서 DAG가 보이지 않는 문제를 배포 전에
잡을 수 있습니다.

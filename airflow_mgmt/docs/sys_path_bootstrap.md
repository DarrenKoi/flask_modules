# `sys.path` bootstrap: DAG가 자기 코드를 찾는 방식

이 문서는 이 repo의 DAG 파일과 entry-point script 상단에 두는 작은 bootstrap stub을
설명합니다. 공유 `runtime_paths.py` 모듈은 없으며, 이 stub 자체가 전체 메커니즘입니다.

---

## 문제

Apache Airflow의 dag-processor는 설정된 DAGs folder 아래의 각 `.py` 파일을 package의
일부가 아니라 **top-level script**로 parse합니다. 그래서 다음 문제가 생깁니다.

1. 상대 import(`from .helpers import x`)는 parent package가 없어서 실패합니다.
2. dag-processor의 `sys.path`에는 DAGs folder와 Airflow 자체 경로가 들어 있지만,
   repo의 나머지 경로가 **항상** 들어 있다고 보장할 수 없습니다. worker가
   `/ops/airflow`에서 시작될 수도 있고, checkout이 `/srv/repos/airflow_mgmt/`에 있을
   수도 있으며, `sys.path`에는 `dags/` folder만 들어 있을 수 있습니다.
3. DAG import가 실패하면 UI에 오류와 함께 보이는 것이 아니라 조용히 사라질 수
   있습니다. bootstrap 실패는 운영에서 디버깅하기 어렵습니다.

해결책은 단순합니다. **repo-local package를 import하기 전에 `airflow_mgmt/`가
`sys.path`에 들어 있는지 확인합니다.** 그러면 `scripts/`, `minio_handler/`, 추후
추가될 repo-local package를 top-level 이름으로 import할 수 있고, DAG, `pytest`,
standalone script 모두에서 같은 `from minio_handler import MinioObject`가 동작합니다.

---

## Bootstrap stub 구조

이 block은 repo-local package를 import해야 하는 모든 DAG 파일 또는 entry-point script
상단에 둡니다. 의도적으로 파일마다 중복합니다. 이유는 아래의 "공유 모듈로 빼지 않는
이유"에서 설명합니다.

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

# repo-local import는 bootstrap 뒤에 둡니다.
from minio_handler import MinioObject     # noqa: E402
```

marker 파일은 `airflow_mgmt/project_root.txt`입니다. 이 파일의 유일한 역할은 존재하는
것입니다. 파일에는 사람이 열었을 때 목적을 이해할 수 있도록 `do not remove`라는
문구가 들어 있지만, bootstrap은 내용을 읽지 않고 `is_file()` 여부만 확인합니다.
검색은 `__file__`에서 parent directory를 위로 올라가며 marker를 **포함한** 첫 폴더에서
멈춥니다. parent folder 이름은 `airflow_mgmt/`, `repo/`, `dags_repo/`, 또는
`/opt/airflow/dags/repo/...`처럼 무엇이든 상관없고, 내부에 marker만 있으면 됩니다.

같은 `project_root.txt` 규칙은 `sys.path`나 "repo root 찾기"가 필요한 다른 project에도
쓸 수 있습니다. project root에 파일을 두고 parent directory를 올라가며 찾으면 됩니다.

동작을 줄별로 보면 다음과 같습니다.

1. **`os.getenv("AIRFLOW_MGMT_ROOT")`**: env override가 우선합니다. 변수가 설정되어
   있으면 그 값을 그대로 사용하고 auto-detect는 하지 않습니다. 변수가 없으면 `None`을
   반환합니다.
2. **`or next(...)`**: env 값이 없으면 현재 파일에서 위로 올라가며
   `project_root.txt` marker를 가진 parent directory를 찾습니다. parent directory
   이름을 보지 않고 marker만 보기 때문에 rename에 안전합니다.
3. **`""` 기본값**: parent walk에서 아무것도 찾지 못하면 `next(...)`가 `""`를
   반환하고, 이는 `Path("").resolve()`를 거쳐 `cwd`가 됩니다. 다음 검사가 이 상황을
   잡습니다.
4. **`if not ROOT_DIR.is_dir(): raise`**: task 실행 중이 아니라 **parse time**에 크게
   실패시킵니다. dag-processor log에서 보는 편이 task 중간의 `ModuleNotFoundError`보다
   훨씬 찾기 쉽습니다.
5. **`sys.path.insert(0, str(ROOT_DIR))`**: `airflow_mgmt/`를 맨 앞에 넣어 worker에
   같은 이름의 다른 module이 있어도 이 repo의 package가 먼저 잡히게 합니다.
6. **`# noqa: E402`**: bootstrap 이후 import는 flake8에서 "module-level import not at
   top of file"로 표시될 수 있습니다. bootstrap이 먼저 실행되어야 하므로 의도된
   예외입니다.

### Folder 이름 대신 marker 파일을 쓰는 이유

이 stub의 예전 버전은 parent 이름이 정확히 `airflow_mgmt`인지를 찾았습니다. 이 방식은
두 가지 상황에서 깨졌습니다.

1. **Clone 또는 container packaging 중 이름 변경**: `git clone <url> repo`처럼 clone하거나
   Airflow git-sync가 checkout을 `/opt/airflow/dags/repo/`로 mount하면 ancestor 중
   `airflow_mgmt`라는 이름이 없어서 자동 탐지가 실패하고 env override가 필수가 됩니다.
2. **모호한 ancestor**: monorepo folder나 packaging staging dir처럼 관련 없는 parent
   directory 이름도 `airflow_mgmt`라면 잘못된 위치에서 walk가 멈춥니다.

sentinel 파일을 쓰면 두 문제가 모두 사라집니다. 우리가 제어하는 directory는 marker가
있는 directory 하나뿐입니다. Git의 `.git/`, Bazel의 `WORKSPACE`, Python tooling의
`pyproject.toml`과 같은 패턴입니다.

---

## 전체 흐름: DAG → script → helper

```text
                          [bootstrap stub]
                                 │
                                 ▼
airflow_mgmt/  ──── sys.path에 추가 ────►  Python이 이제 import 가능
    │                                         scripts/, minio_handler/, ...
    │
    ├── dags/<topic>/foo_dag.py
    │       [bootstrap stub]
    │       from scripts.ftp_download_sample import collect_logs  ◄─ repo-local
    │       @task                                                    import
    │       def download_and_upload(ips): return collect_logs(ips)
    │
    ├── scripts/
    │   └── ftp_download_sample.py
    │       [bootstrap stub]
    │       from minio_handler import MinioObject     ◄─── 또 다른 repo-local
    │       def collect_logs(ips: list[str]) -> dict: ...    import
    │
    └── minio_handler/
        ├── __init__.py
        └── object.py    (MinioObject 정의)
```

이 세 단계는 모두 같은 `sys.path.insert(0, airflow_mgmt/)`로 가능해집니다.

1. DAG 파일이 bootstrap을 실행한 뒤 `scripts.ftp_download_sample.collect_logs`를
   import합니다.
2. `scripts/ftp_download_sample.py`도 bootstrap을 실행합니다. 이미 경로가 들어 있으면
   두 번째 `sys.path.insert`는 idempotent하므로 사실상 비용이 작은 no-op입니다. 다만
   script를 standalone으로 실행할 때(`python scripts/ftp_download_sample.py`) 필요합니다.
3. `minio_handler/object.py`는 bootstrap하지 않습니다. 이 파일이 import되는 시점에는
   호출자가 이미 `sys.path`를 준비한 뒤입니다.

기준은 단순합니다. **entry point인 파일에만 stub이 필요합니다.** DAG 파일은
dag-processor가 top-level script로 parse하므로 entry point입니다. standalone CLI인
`scripts/ftp_download_sample.py`도 entry point입니다. import만 되는 순수 helper module은
entry point가 아니며, 호출자의 `sys.path`를 그대로 물려받습니다.

---

## Bootstrap하지 않는 경우

모든 DAG에 stub이 필요한 것은 아닙니다. 기준은 다음과 같습니다.

> DAG 파일이 `scripts/`, `minio_handler/`, 또는 다른 repo-local package를 import하지
> 않는다면 **bootstrap stub을 넣지 않습니다**.

`dags/diagnostics/inspect_packages_dag.py`가 이 기준의 예입니다. 이 파일은 stdlib
(`importlib.metadata.distributions`)와 `airflow.sdk`만 사용하므로 Airflow가 제공하는
`sys.path`만으로 parse됩니다. 여기에 stub을 넣으면 얻는 것 없이 parse-time 실패
가능성만 늘어납니다.

---

## 두 경로: `ROOT_DIR`(code)와 `SCRATCH_ROOT`(data)

"code가 있는 위치"와 "파일을 쓰는 위치"를 섞으면 `PermissionError: cannot mkdir under
the dags folder` 같은 문제가 생깁니다.

| 변수 | 설정 위치 | Airflow에서 가리키는 곳 | local dev에서 가리키는 곳 | 용도 |
|---|---|---|---|---|
| `ROOT_DIR` | bootstrap stub | `/opt/airflow/dags/repo/airflow_mgmt/` (read-only git mount) | `airflow_mgmt/` (checkout) | bundle된 config 파일 찾기, **write 금지** |
| `SCRATCH_ROOT` | `scripts/ftp_download_sample.py`의 `_scratch_root()` | `/tmp/airflow_mgmt/` | `airflow_mgmt/scratch/` | download, 작업 파일, `mkdir` 또는 write 대상 |

왜 둘을 나눌까요? 관리형 Airflow cluster에서 worker는 보통 git mount를 **read**할 수는
있지만, source tree가 오염되지 않도록 **write**는 막혀 있습니다. 그래서 scratch는
다른 곳을 써야 합니다.

현재는 `scripts/ftp_download_sample.py`만 scratch 처리가 필요하므로 helper가 그 파일
안에 inline으로 있습니다. 두 번째 script에서도 필요해지면 그때 상황에 맞게 helper를
복사하거나 공유 module로 분리합니다.

---

## 환경 변수

```text
AIRFLOW_MGMT_ROOT          → airflow_mgmt/ 절대 경로 (bootstrap stub에서 사용)
AIRFLOW_MGMT_SCRATCH_ROOT  → write 가능한 runtime directory 절대 경로 (ftp_download_sample.py에서 사용)
```

두 값은 **OS 환경 변수**입니다. Airflow Variables도 아니고 config file entry도 아닙니다.
코드에서는 `os.getenv(...)`로 읽습니다. 설정 예시는 다음과 같습니다.

**PowerShell (현재 세션):**

```powershell
$env:AIRFLOW_MGMT_ROOT = "C:\Code\flask_modules\airflow_mgmt"
$env:AIRFLOW_MGMT_SCRATCH_ROOT = "D:\airflow_scratch"
```

**PowerShell (사용자 영구 설정):**

```powershell
[Environment]::SetEnvironmentVariable("AIRFLOW_MGMT_ROOT", "C:\Code\flask_modules\airflow_mgmt", "User")
```

**bash / zsh shell 설정:**

```bash
export AIRFLOW_MGMT_ROOT=/srv/repos/airflow_mgmt
```

**Airflow cluster 설정:**

ops가 systemd unit, Kubernetes Pod spec, Helm values, docker-compose `environment:` block 등
scheduler, dag-processor, worker process가 시작되는 위치에 이 값을 설정합니다.

`AIRFLOW_MGMT_ROOT`는 auto-detect가 실패할 때만 설정합니다. 보통 marker가 rename/delete
되었거나, bootstrap을 실행하는 파일이 실제 project root의 descendant가 아닌 경우입니다.
예를 들어 Airflow worker가 parent 없이 `dags/`만 mount하면 parent walk로
`project_root.txt`를 찾을 수 없습니다.

`AIRFLOW_MGMT_SCRATCH_ROOT`는 `/tmp`가 너무 작거나 느리거나 다른 volume이어야 할 때
설정합니다.

---

## 피해야 할 패턴

1. **`sys.path.append("/some/hard/coded/path")`**: 한 Airflow cluster에서는 동작해도
   다음 cluster에서 깨집니다. stub을 사용합니다.
2. **`airflow_mgmt/project_root.txt` 삭제 또는 rename**: auto-detect walk가 찾는
   sentinel입니다. 이 파일이 없으면 stub은 `cwd`로 fallback한 뒤 parse time에
   `RuntimeError`를 냅니다. 꼭 제거해야 한다면 모든 worker에 `AIRFLOW_MGMT_ROOT`를
   명시적으로 설정해야 합니다.
3. **DAG 파일에서 `from .helpers import x` 사용**: dag-processor가 각 DAG를 package의
   일부가 아닌 top-level script로 parse하므로 상대 import는 실패합니다.
4. **`ROOT_DIR` 아래에 write**: Airflow worker에서는 read-only git mount입니다.
   `mkdir`나 `open(..., "w")`가 필요한 파일은 `SCRATCH_ROOT`를 사용합니다.
5. **필요 없는 DAG에 stub 추가**: repo-local import가 없으면 stub을 넣지 않습니다.
   얻는 것 없이 parse-time 실패 가능성만 추가됩니다.
6. **bootstrap 이후 import에 `# noqa: E402` 누락**: flake8이 표시할 수 있습니다.
   bootstrap은 repo-local import보다 먼저 실행되어야 하므로 `noqa`는 의도된 예외입니다.
7. **helper module에서 Airflow 내부에 의존**(`minio_handler/`, 추후 `utils/` 등):
   helper layer를 Airflow-free로 유지해야 DAG layer만 Airflow API 변화에 맞춰 조정하면
   됩니다.

---

## 공유 모듈로 빼지 않는 이유

stub을 5개 파일에 중복하지 않고 helper module로 빼고 싶을 수 있습니다. 하지만 여기에는
순환 문제가 있습니다.

> `from helpers.bootstrap import setup_path`를 하려면 `helpers.bootstrap`가 이미 import
> 가능해야 합니다. 그런데 helper를 import 가능하게 만드는 일이 `setup_path`의
> 목적입니다.

따라서 최초의 `sys.path.insert`는 각 entry-point file 안에 있어야 합니다. 그 이후의
추가 로직은 공유 module로 뺄 수 있지만, 현재 공유할 만한 로직은 `~3 lines`뿐입니다.
그 정도 코드를 위한 module은 도움보다 우회가 더 커집니다.

template도 **copy-paste 가능**해야 합니다.
`dag_templates/taskflow_decorator_template.py`나 `dag_templates/virtualenv_task_template.py`를
`dags/<topic>/foo_dag.py`로 옮기면 다른 파일을 건드리지 않아도 동작하는 DAG가 되어야
합니다. 공유 helper module을 요구하면
template 복사 작업마다 그 module 경로도 같이 챙겨야 해서 template의 목적과 맞지
않습니다.

bootstrap 로직이 세 번째, 네 번째로 늘어나면 다시 검토할 수 있습니다. 지금은
entry-point file마다 stub을 직접 두는 방식이 가장 읽기 쉬운 trade-off입니다.

---

## 검증 방법

```bash
# DAG 무결성: 모든 DAG 파일이 정상 parse되는지 확인
python -m pytest airflow_mgmt/tests/test_dag_integrity.py -v
```

운영 UI에서 DAG가 조용히 사라지면 import 실패일 가능성이 큽니다. 위 test를 local에서
실행하면 parse error를 재현하고 어떤 파일이 문제인지 확인할 수 있습니다.

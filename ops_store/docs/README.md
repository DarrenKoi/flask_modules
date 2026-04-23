# ops_store 문서

이 폴더는 `ops_store` 패키지와 함께 들어 있는 작은 Flask service를
설명합니다.

이 폴더의 문서:

- `codebase.md`: repository 구조, module 역할, runtime 흐름 설명
- `usage.md`: 설정 방법, 예제, 권장 사용 패턴, 자주 하는 실수 정리
- `policy.md`: ISM policy의 state/action/transition 개념과
  `create_ism_policy` 해설, four-tier lifecycle 예제
- `logging_strategy.md`: OpenSearch를 로그 저장소로 쓸 때의 권장 아키텍처와
  운영 전략

프로젝트 구조부터 이해하고 싶다면 `codebase.md`부터 읽는 것이 좋습니다.
바로 OpenSearch helper를 app이나 script에서 사용하고 싶다면 `usage.md`부터
보면 됩니다.

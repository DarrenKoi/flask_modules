# OpenSearch를 로그 저장소로 쓸 때의 전략

## 짧은 결론

가장 안전한 전략은 아래와 같습니다.

1. application은 먼저 local file 또는 stdout에 structured log를 남깁니다.
2. 별도의 log collector가 그 로그를 읽어 OpenSearch로 전달합니다.
3. OpenSearch 안에서는 application data index와 분리된 전용 log index 또는
   data stream을 사용합니다.
4. rollover, retention, access control도 log 전용 정책으로 따로 운영합니다.

즉, application process가 request 처리 중에 OpenSearch에 직접 log를 쓰는
방식은 기본 전략으로 두지 않는 편이 좋습니다.

## 왜 direct write를 기본값으로 두지 않는가

OpenSearch를 log 저장소로 쓰고 싶을 때 가장 먼저 피해야 할 것은,
request path 안에서 logging call이 곧바로 OpenSearch write에 의존하게 만드는
구성입니다.

이 방식은 다음 문제가 있습니다.

- OpenSearch가 느리거나 장애가 나면 application logging도 같이 막힘
- request latency가 logging storage 상태에 영향을 받음
- logging 실패가 application 예외 처리와 섞이기 쉬움
- 재시도, buffering, backpressure를 app 코드가 직접 떠안게 됨
- 같은 cluster를 business search와 logging에 같이 쓰면 burst traffic 때 서로
  간섭하기 쉬움

logging은 보조 기능이지만, 운영에서는 장애 순간에 가장 필요합니다.
그래서 primary write path는 최대한 단순하고 local이어야 합니다.

## 권장 아키텍처

권장 순서는 아래와 같습니다.

### 1. app은 local file 또는 stdout에 먼저 기록

- Flask app log는 root `logging_config.py`의 `setup_logger(path_dir, name)` 또는
  `configure_flask_logging(...)`으로 파일에 기록
- 가능하면 JSON 형태의 structured log를 사용

이 단계의 핵심은 "OpenSearch가 없어도 log가 남아야 한다"는 점입니다.

### 2. collector가 비동기적으로 OpenSearch에 전달

추천되는 역할 분리는 아래와 같습니다.

- app process: 로그 생성
- collector: tailing, batching, retry, buffering
- OpenSearch: 검색과 보관

대표적인 collector 예시는 Fluent Bit, Vector, Filebeat, Logstash입니다.

collector를 두면 다음 이점이 있습니다.

- bulk indexing을 collector가 대신 처리
- OpenSearch 장애 시 disk buffer나 retry 전략을 collector에서 관리 가능
- 여러 process나 여러 host의 로그를 한 경로로 모을 수 있음
- app 코드에 OpenSearch write retry 로직을 넣지 않아도 됨

### 3. OpenSearch에는 log 전용 저장소 사용

가능하면 아래 둘 중 하나를 선택합니다.

- OpenSearch data stream
- alias + backing index + rollover

이 repository 기준으로는 `ops_store.index.OSIndex.rollover()`가 이미 있으므로,
현재 helper 범위 안에서 설명하기 쉬운 쪽은 alias + backing index 패턴입니다.

예:

- write alias: `logs-flask-modules`
- backing index: `logs-flask-modules-000001`
- 다음 backing index: `logs-flask-modules-000002`

이렇게 하면 application이나 collector는 항상 stable alias만 바라보고,
실제 index 교체는 rollover로 처리할 수 있습니다.

## index / mapping 설계 원칙

logging index는 business document index와 요구사항이 다릅니다.

최소한 아래 필드는 고정적으로 가지는 편이 좋습니다.

- `@timestamp`: event 발생 시각
- `level`: `INFO`, `ERROR` 같은 log level
- `logger`: logger name
- `service`: service 이름
- `env`: `local`, `dev`, `prod` 같은 환경명
- `host`: host name 또는 container/pod 식별자
- `message`: 사람이 읽는 message
- `request_id`: request correlation id
- `trace_id`, `span_id`: tracing 연동 시
- `module`, `pathname`, `lineno`
- `exception.type`, `exception.message`, `exception.stack`

실무적으로는 아래 원칙이 중요합니다.

- 자주 filter하는 값은 `keyword`
- 전체 검색이 필요한 message는 `text` + 필요 시 `keyword` subfield
- timestamp는 반드시 date 계열
- 고카디널리티 field를 무분별하게 늘리지 않기
- request body 전체를 그대로 넣기 전에 민감정보와 비용을 먼저 점검

## retention / rollover 전략

logging data는 계속 쌓이므로 보관 정책이 핵심입니다.

기본 원칙:

- business index와 retention 정책을 분리
- 용량 기준 또는 기간 기준으로 rollover
- 오래된 backing index는 자동 삭제
- replica 수와 refresh interval도 log workload 기준으로 조정

보통 log는 write-heavy, append-only workload에 가깝기 때문에:

- refresh interval을 너무 짧게 둘 필요는 없음
- shard 수를 과도하게 늘리지 않는 편이 좋음
- retention을 먼저 정하고 그에 맞춰 storage cost를 계산해야 함

작은 서비스라면 하루 단위 또는 용량 기준 rollover 중 하나로 시작하고,
운영 중 실제 volume을 본 뒤 조정하는 접근이 현실적입니다.

## security / compliance 원칙

log는 편하지만, 그대로 두면 가장 쉽게 민감정보가 새는 경로이기도 합니다.

최소한 아래는 지키는 편이 좋습니다.

- password, token, secret, cookie, authorization header는 기록하지 않기
- 사용자 입력 전체를 raw로 남기기 전에 redaction 규칙 적용
- OpenSearch write 계정은 log index에만 쓰기 권한 부여
- 운영자 조회 권한도 business data와 log data를 분리

## 이 repository에 맞춘 현실적인 추천안

이 repo에서는 아래 구성이 가장 무난합니다.

1. Flask app log는 root `logging_config.py`로 `logs/flask/server.log` 같은 파일에
   기록
2. collector가 `logs/flask/*.log`를 수집
3. OpenSearch에는 `logs-flask-modules` 같은 전용 alias 또는 data stream으로
   적재
4. rollover와 retention은 log 전용 정책으로 운영

`ops_store`는 OpenSearch 호출을 자체 로깅하지 않습니다. 클러스터 상태와
쿼리 성능은 OpenSearch/Kibana 대시보드 또는 별도 monitoring 서비스로
관찰하는 것을 전제로 합니다.

이 방식의 장점:

- app code는 logging storage 장애에 덜 민감
- direct OpenSearch logging handler를 app 코드에 심지 않아도 됨

## direct OpenSearch logging이 꼭 필요할 때

특별한 이유로 process 안에서 곧바로 OpenSearch에 넣어야 한다면,
최소한 아래 제약을 두는 편이 좋습니다.

- main request thread에서 바로 전송하지 말고 queue 기반 비동기 처리
- 단건 index 대신 batch / bulk 전송
- 실패 시 local file fallback 유지
- log write용 client와 business query용 client를 분리
- log index prefix, credentials, timeout을 분리
- logging 내부 예외가 application exception을 덮지 않게 보호

즉, direct OpenSearch logging은 "가능한 기본값"이 아니라
"명확한 이유가 있을 때 제한적으로 쓰는 예외 전략"에 가깝습니다.

## `ops_store`와의 관계

`ops_store`는 OpenSearch document/index/search helper 역할에만 집중하고,
logging transport 역할은 가지지 않습니다.

- app / library는 Python logging으로 local log를 남기고
- collector가 OpenSearch로 배송하며
- OpenSearch 클러스터 상태는 Kibana 또는 별도 monitoring 서비스로 관찰

이렇게 역할을 나누는 편이 운영상 더 안정적입니다.

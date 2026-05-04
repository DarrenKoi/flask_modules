# minio_handler 문서

이 폴더는 `minio_handler` 패키지의 사용법과 MinIO 자체에 대한 학습 자료를
함께 정리합니다.

이 폴더의 문서:

- `concepts.md`: object storage / S3 / MinIO 핵심 개념. bucket, key,
  metadata, versioning, consistency, multi-tenancy, 권한 모델 정리
- `data_management.md`: key naming, 시간 기반 partitioning, content-addressable
  storage, tag vs metadata vs prefix, retention 패턴, soft delete
- `web_integration.md`: Flask + MinIO 통합 패턴. backend proxy / presigned
  URL / multipart upload / 비동기 처리 / CORS / 보안 함정
- `usage.md`: `minio_handler` wrapper 자체의 설정 방법, CRUD 예제, presigned
  URL, lifecycle wrapper, 자주 하는 실수 정리

처음 보는 거라면 `concepts.md` → `usage.md` → 필요에 따라 `data_management.md`
혹은 `web_integration.md` 순으로 읽으면 좋습니다.

`minio_handler`는 `minio` Python SDK 위에 얇게 올린 wrapper입니다. bucket /
object key prefix를 instance 단위로 보관해 주고, 자주 쓰는 read / write /
update / delete / list / presigned URL / lifecycle 호출을 한 줄로 만들어
줍니다.

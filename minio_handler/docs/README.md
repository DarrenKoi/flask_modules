# minio_handler 문서

이 폴더는 `minio_handler` 패키지의 사용법을 정리합니다.

이 폴더의 문서:

- `usage.md`: 설정 방법, CRUD 예제, presigned URL, Flask 연동 패턴, 자주 하는
  실수 정리

`minio_handler`는 `minio` Python SDK 위에 얇게 올린 wrapper입니다. bucket /
object key prefix를 instance 단위로 보관해 주고, 자주 쓰는 read / write /
update / delete / list / presigned URL 호출을 한 줄로 만들어 줍니다.

"""Object-level CRUD operations against a MinIO / S3-compatible bucket."""

import io
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, BinaryIO

from .base import MinioBase


def _delete_object_class() -> type[Any]:
    from minio.deleteobjects import DeleteObject

    return DeleteObject


class MinioObject(MinioBase):
    """File-style CRUD wrapper for MinIO objects.

    S3/MinIO has no in-place update — ``put`` and ``upload`` overwrite when
    the key already exists, which serves as both create and update.
    """

    def put(
        self,
        key: str,
        data: bytes | bytearray | memoryview | BinaryIO,
        *,
        bucket: str | None = None,
        length: int | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        part_size: int = 0,
    ) -> Any:
        """Write raw bytes or a binary stream to ``key``.

        For streams without a known size, pass ``length=-1`` and a non-zero
        ``part_size`` (e.g. 10 * 1024 * 1024).
        """

        bucket_name = self._resolve_bucket(bucket)
        full_key = self._resolve_key(key)

        if isinstance(data, (bytes, bytearray, memoryview)):
            buffer = bytes(data)
            stream: BinaryIO = io.BytesIO(buffer)
            data_length = len(buffer)
        else:
            stream = data
            data_length = -1 if length is None else length

        return self.client.put_object(
            bucket_name,
            full_key,
            stream,
            data_length,
            content_type=content_type,
            metadata=metadata,
            part_size=part_size,
        )

    def upload(
        self,
        key: str,
        file_path: str | Path,
        *,
        bucket: str | None = None,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        part_size: int = 0,
    ) -> Any:
        """Upload a local file to ``key``."""

        bucket_name = self._resolve_bucket(bucket)
        full_key = self._resolve_key(key)
        source = Path(file_path)

        return self.client.fput_object(
            bucket_name,
            full_key,
            str(source),
            content_type=content_type,
            metadata=metadata,
            part_size=part_size,
        )

    def get(
        self,
        key: str,
        *,
        bucket: str | None = None,
        offset: int = 0,
        length: int = 0,
    ) -> bytes:
        """Read object body and return raw bytes."""

        bucket_name = self._resolve_bucket(bucket)
        full_key = self._resolve_key(key)

        response = self.client.get_object(
            bucket_name,
            full_key,
            offset=offset,
            length=length,
        )
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def download(
        self,
        key: str,
        file_path: str | Path,
        *,
        bucket: str | None = None,
    ) -> Path:
        """Download ``key`` to a local file path. Returns the destination path."""

        bucket_name = self._resolve_bucket(bucket)
        full_key = self._resolve_key(key)
        destination = Path(file_path)
        destination.parent.mkdir(parents=True, exist_ok=True)

        self.client.fget_object(bucket_name, full_key, str(destination))
        return destination

    def stat(self, key: str, *, bucket: str | None = None) -> Any:
        """Return object metadata (size, etag, content_type, last_modified, ...)."""

        bucket_name = self._resolve_bucket(bucket)
        full_key = self._resolve_key(key)
        return self.client.stat_object(bucket_name, full_key)

    def exists(self, key: str, *, bucket: str | None = None) -> bool:
        """Return ``True`` if the object exists, ``False`` otherwise."""

        from minio.error import S3Error

        try:
            self.stat(key, bucket=bucket)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                return False
            raise
        return True

    def delete(self, key: str, *, bucket: str | None = None) -> None:
        """Delete a single object."""

        bucket_name = self._resolve_bucket(bucket)
        full_key = self._resolve_key(key)
        self.client.remove_object(bucket_name, full_key)

    def delete_many(
        self,
        keys: Iterable[str],
        *,
        bucket: str | None = None,
    ) -> list[Any]:
        """Delete multiple objects in one request. Returns any error entries."""

        bucket_name = self._resolve_bucket(bucket)
        delete_object = _delete_object_class()
        targets = [delete_object(self._resolve_key(k)) for k in keys]
        if not targets:
            return []
        return list(self.client.remove_objects(bucket_name, targets))

    def list(
        self,
        prefix: str | None = None,
        *,
        bucket: str | None = None,
        recursive: bool = True,
        start_after: str | None = None,
    ) -> Iterator[Any]:
        """Yield objects under ``prefix`` (combined with the default prefix)."""

        bucket_name = self._resolve_bucket(bucket)
        scoped_prefix = self._resolve_key(prefix) if prefix else self.default_prefix

        kwargs: dict[str, Any] = {
            "bucket_name": bucket_name,
            "recursive": recursive,
        }
        if scoped_prefix:
            kwargs["prefix"] = scoped_prefix.rstrip("/") + "/"
        if start_after is not None:
            kwargs["start_after"] = start_after

        return self.client.list_objects(**kwargs)

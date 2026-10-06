from typing import Any

import boto3
from botocore.exceptions import ClientError


class S3BlobStore:
    """BlobStore over one bucket. Objects inherit the bucket's default SSE-KMS encryption."""

    def __init__(self, bucket: str, client: Any = None) -> None:
        self._bucket = bucket
        self._s3 = client or boto3.client("s3")

    def get(self, key: str) -> bytes:
        body: bytes = self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        return body

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=key)

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        for page in self._s3.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=prefix
        ):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys

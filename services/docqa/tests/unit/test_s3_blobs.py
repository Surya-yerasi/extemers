import io

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from docqa.adapters.s3_blobs import S3BlobStore

BUCKET = "docqa-test"


@pytest.fixture
def s3() -> tuple[S3BlobStore, Stubber]:
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    stub = Stubber(client)
    stub.activate()
    return S3BlobStore(BUCKET, client), stub


def test_get(s3: tuple[S3BlobStore, Stubber]) -> None:
    store, stub = s3
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(b"pdf"), 3)},
        {"Bucket": BUCKET, "Key": "raw/a.pdf"},
    )
    assert store.get("raw/a.pdf") == b"pdf"


def test_put(s3: tuple[S3BlobStore, Stubber]) -> None:
    store, stub = s3
    stub.add_response(
        "put_object",
        {},
        {
            "Bucket": BUCKET,
            "Key": "parsed/a.json",
            "Body": b"{}",
            "ContentType": "application/json",
        },
    )
    store.put("parsed/a.json", b"{}", "application/json")
    stub.assert_no_pending_responses()


@pytest.mark.parametrize(("code", "expected"), [("404", False), ("NoSuchKey", False)])
def test_exists_missing(s3: tuple[S3BlobStore, Stubber], code: str, expected: bool) -> None:
    store, stub = s3
    stub.add_client_error("head_object", service_error_code=code, http_status_code=404)
    assert store.exists("parsed/x.json") is expected


def test_exists_present(s3: tuple[S3BlobStore, Stubber]) -> None:
    store, stub = s3
    stub.add_response("head_object", {}, {"Bucket": BUCKET, "Key": "parsed/x.json"})
    assert store.exists("parsed/x.json") is True


def test_exists_reraises_other_errors(s3: tuple[S3BlobStore, Stubber]) -> None:
    store, stub = s3
    stub.add_client_error("head_object", service_error_code="AccessDenied", http_status_code=403)
    with pytest.raises(ClientError):
        store.exists("parsed/x.json")


def test_delete(s3: tuple[S3BlobStore, Stubber]) -> None:
    store, stub = s3
    stub.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": "parsed/x.json"})
    store.delete("parsed/x.json")
    stub.assert_no_pending_responses()


def test_list_keys_paginates(s3: tuple[S3BlobStore, Stubber]) -> None:
    store, stub = s3
    stub.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "raw/a.pdf"}], "IsTruncated": True, "NextContinuationToken": "t"},
        {"Bucket": BUCKET, "Prefix": "raw/"},
    )
    stub.add_response(
        "list_objects_v2",
        {"Contents": [{"Key": "raw/b.png"}], "IsTruncated": False},
        {"Bucket": BUCKET, "Prefix": "raw/", "ContinuationToken": "t"},
    )
    assert store.list_keys("raw/") == ["raw/a.pdf", "raw/b.png"]

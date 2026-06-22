"""Object store client — uploads run artifacts to MinIO (local) or S3 (prod).

All storage configuration comes from environment variables (.env):

    STORAGE_BACKEND=minio|s3|disabled
    STORAGE_BUCKET=flowprobe-evidence
    STORAGE_ENDPOINT_URL=http://localhost:9000   # MinIO only; omit for S3

    MinIO credentials:  MINIO_ACCESS_KEY / MINIO_SECRET_KEY
    S3 credentials:     AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY

Public API
----------
upload_run_downloads(run_id, config) -> dict[str, str]
    Uploads every file in evidence/{run_id}/downloads/ and returns a dict
    mapping filename → presigned URL ready to embed in the report.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from flowprobe.config import settings
from flowprobe.logger import get_logger

log = get_logger("object_store")


def _make_client() -> Any:
    """Return a boto3 S3 client. endpoint_url drives the target — blank = real AWS S3."""
    import boto3  # type: ignore

    return boto3.client(
        "s3",
        endpoint_url=settings.storage_endpoint_url or None,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
    )


def _ensure_bucket(client: Any, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:
        try:
            client.create_bucket(Bucket=bucket)
            log.info("object_store: created bucket '%s'", bucket)
        except Exception as exc:
            raise RuntimeError(f"object_store: cannot access or create bucket '{bucket}': {exc}") from exc


def upload_run_downloads(run_id: str) -> dict[str, str]:
    """Upload all files in evidence/{run_id}/downloads/ to object storage.

    Returns a dict mapping filename → presigned URL:
        {"ExportedRequisitions.xlsx": "https://..."}

    Returns {} if STORAGE_BUCKET is not set or the downloads directory is empty.
    """
    if not settings.storage_bucket:
        log.info("object_store: STORAGE_BUCKET not set — skipping upload for run %s", run_id)
        return {}

    evidence_dir = Path(settings.evidence_output_dir)
    downloads_dir = evidence_dir / run_id / "downloads"

    if not downloads_dir.exists():
        log.info("object_store: no downloads dir for run %s — skipping upload", run_id)
        return {}

    files = [f for f in downloads_dir.iterdir() if f.is_file()]
    if not files:
        log.info("object_store: downloads dir empty for run %s — skipping upload", run_id)
        return {}

    client = _make_client()
    if client is None:
        return {}

    bucket = settings.storage_bucket
    expiry = settings.storage_presign_expiry_seconds

    _ensure_bucket(client, bucket)

    url_map: dict[str, str] = {}
    for file_path in files:
        s3_key = f"{run_id}/downloads/{file_path.name}"
        try:
            client.upload_file(str(file_path), bucket, s3_key)
            url = client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": s3_key},
                ExpiresIn=expiry,
            )
            url_map[file_path.name] = url
            log.info("object_store: uploaded %s → s3://%s/%s", file_path.name, bucket, s3_key)
        except Exception as exc:
            log.warning("object_store: upload failed for %s: %s", file_path.name, exc)

    return url_map

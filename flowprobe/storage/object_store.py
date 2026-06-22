"""Object store client — uploads run artifacts to MinIO (local) or S3 (prod).

Reads config from the ``storage`` block in config.yaml:

    storage:
      backend: minio          # minio | s3 | disabled
      bucket: flowprobe-evidence
      endpoint_url: "http://localhost:9000"   # minio only
      presign_expiry_seconds: 604800          # 7 days
      # credentials via env: MINIO_ACCESS_KEY / MINIO_SECRET_KEY  (minio)
      #                   or  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  (s3)

Public API
----------
upload_run_downloads(run_id, config) -> list[dict]
    Uploads every file in evidence/{run_id}/downloads/ and returns a list of
    {filename, s3_key, url} dicts ready to embed in the report.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from flowprobe.logger import get_logger

log = get_logger("object_store")


def _make_client(storage_cfg: dict) -> Any:
    """Return a boto3 S3 client configured for MinIO or AWS S3."""
    import boto3  # type: ignore

    backend = storage_cfg.get("backend", "disabled").lower()
    if backend == "minio":
        return boto3.client(
            "s3",
            endpoint_url=storage_cfg["endpoint_url"],
            aws_access_key_id=os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
            aws_secret_access_key=os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
        )
    if backend == "s3":
        return boto3.client("s3")  # uses AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from env
    return None


def _ensure_bucket(client: Any, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:
        try:
            client.create_bucket(Bucket=bucket)
            log.info("object_store: created bucket '%s'", bucket)
        except Exception as exc:
            raise RuntimeError(f"object_store: cannot access or create bucket '{bucket}': {exc}") from exc


def upload_run_downloads(run_id: str, config: dict) -> dict[str, str]:
    """Upload all files in evidence/{run_id}/downloads/ to object storage.

    Returns a dict mapping filename → presigned URL:
        {"ExportedRequisitions.xlsx": "https://..."}

    Returns {} if storage is disabled or the downloads directory is empty.
    """
    storage_cfg = config.get("storage", {})
    backend = storage_cfg.get("backend", "disabled").lower()

    if backend == "disabled":
        log.info("object_store: storage disabled — skipping upload for run %s", run_id)
        return {}

    evidence_dir = Path(config.get("evidence", {}).get("output_dir", "evidence"))
    downloads_dir = evidence_dir / run_id / "downloads"

    if not downloads_dir.exists():
        log.info("object_store: no downloads dir for run %s — skipping upload", run_id)
        return {}

    files = [f for f in downloads_dir.iterdir() if f.is_file()]
    if not files:
        log.info("object_store: downloads dir empty for run %s — skipping upload", run_id)
        return {}

    client = _make_client(storage_cfg)
    if client is None:
        return {}

    bucket = storage_cfg.get("bucket", "flowprobe-evidence")
    expiry = int(storage_cfg.get("presign_expiry_seconds", 604800))

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

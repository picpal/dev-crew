"""R2 Publisher (#7절 15.5) — S3 호환 API로 object 업로드, content hash 검증."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

import boto3


@dataclass(frozen=True)
class PublishResult:
    object_key: str
    content_hash: str
    url: str


class R2Publisher:
    def __init__(self, bucket: str = "dev-crew-reports",
                 public_base: str | None = None):
        account = os.environ["R2_ACCOUNT_ID"]
        self.bucket = bucket
        self.public_base = public_base or os.environ.get("REPORT_BASE_URL", "")
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )

    def publish(self, task_id: str, html: str) -> PublishResult:
        key = f"reports/{task_id}/index.html"
        body = html.encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        head = self._head_hash(key)
        if head != digest:                       # idempotent (§16.2)
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=body,
                               ContentType="text/html; charset=utf-8",
                               Metadata={"content-hash": digest})
        return PublishResult(key, digest, f"{self.public_base}/tasks/{task_id}")

    def _head_hash(self, key: str) -> str | None:
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=key)
            return head.get("Metadata", {}).get("content-hash")
        except self.s3.exceptions.ClientError:
            return None

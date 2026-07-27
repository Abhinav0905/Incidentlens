"""Optional object-storage upload for finished videos.

Backblaze B2 exposes an S3-compatible API, so this uses boto3's S3 client with
B2's endpoint. Credentials come from arguments or the environment. This path is
not exercised by the test suite (it needs a live bucket); the call shape is the
standard S3 ``upload_file`` / ``generate_presigned_url``.
"""

from __future__ import annotations

import os
from pathlib import Path


class BackblazeB2Storage:
    def __init__(
        self,
        bucket: str | None = None,
        endpoint_url: str | None = None,
        key_id: str | None = None,
        application_key: str | None = None,
    ) -> None:
        self.bucket = bucket or os.environ.get("B2_BUCKET", "")
        self.endpoint_url = endpoint_url or os.environ.get("B2_ENDPOINT_URL", "")
        self.key_id = key_id or os.environ.get("B2_KEY_ID", "")
        self.application_key = application_key or os.environ.get("B2_APPLICATION_KEY", "")

    def _client(self) -> object:
        import boto3  # type: ignore[import-untyped]

        if not (self.bucket and self.endpoint_url and self.key_id and self.application_key):
            raise RuntimeError(
                "Backblaze upload needs B2_BUCKET, B2_ENDPOINT_URL, B2_KEY_ID and "
                "B2_APPLICATION_KEY (or the matching arguments)"
            )
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.key_id,
            aws_secret_access_key=self.application_key,
        )

    def upload(self, path: str | Path, key: str | None = None, expires_in: int = 604800) -> str:
        path = Path(path)
        object_key = key or path.name
        client = self._client()
        client.upload_file(  # type: ignore[attr-defined]
            str(path),
            self.bucket,
            object_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
        url: str = client.generate_presigned_url(  # type: ignore[attr-defined]
            "get_object",
            Params={"Bucket": self.bucket, "Key": object_key},
            ExpiresIn=expires_in,
        )
        return url

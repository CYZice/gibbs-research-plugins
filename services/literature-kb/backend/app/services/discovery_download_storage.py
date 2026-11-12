from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app.core.config import Settings


class DiscoveryStorageError(RuntimeError):
    retryable = True


class DiscoveryDownloadStorage:
    """Store validated external PDFs locally or in Tencent COS."""

    def __init__(
        self,
        settings: Settings,
        cos_client=None,
        *,
        provider: str | None = None,
    ) -> None:
        self.settings = settings
        self.provider = (provider or settings.discovery_storage_provider).lower()
        self.root = Path(settings.discovery_download_root).resolve()
        self.cos_client = cos_client

    def store(
        self,
        temporary_path: Path,
        storage_key: str,
        metadata_key: str,
        metadata: dict[str, Any],
    ) -> bool:
        if self.provider == "local":
            return self._store_local(temporary_path, storage_key, metadata_key, metadata)
        if self.provider == "cos":
            return self._store_cos(temporary_path, storage_key, metadata_key, metadata)
        raise DiscoveryStorageError(
            "DISCOVERY_STORAGE_PROVIDER must be configured as 'local' or 'cos'."
        )

    def download_to_path(self, storage_key: str, destination: Path) -> None:
        """Stream one staged PDF into a local ingestion file."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.provider == "local":
                source = self.root / storage_key.removeprefix("downloads/")
                with source.open("rb") as input_stream, destination.open("wb") as output:
                    shutil.copyfileobj(input_stream, output, length=1024 * 1024)
                return
            if self.provider != "cos":
                raise DiscoveryStorageError(
                    "DISCOVERY_STORAGE_PROVIDER must be configured as 'local' or 'cos'."
                )
            response = self._get_cos_client().get_object(
                Bucket=self._require_cos_setting("cos_bucket"),
                Key=self._cos_object_key(storage_key),
            )
            body = response["Body"]
            try:
                raw_stream = body.get_raw_stream() if hasattr(body, "get_raw_stream") else body
                with destination.open("wb") as output:
                    shutil.copyfileobj(raw_stream, output, length=1024 * 1024)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except DiscoveryStorageError:
            raise
        except Exception as exc:
            destination.unlink(missing_ok=True)
            raise DiscoveryStorageError(
                "Could not read the staged PDF from discovery storage."
            ) from exc

    def _store_local(
        self,
        temporary_path: Path,
        storage_key: str,
        metadata_key: str,
        metadata: dict[str, Any],
    ) -> bool:
        staging_path: Path | None = None
        try:
            pdf_path = self.root / storage_key.removeprefix("downloads/")
            metadata_path = self.root / metadata_key.removeprefix("downloads/")
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            duplicate = pdf_path.exists()
            if not duplicate:
                # The downloader's temporary file normally lives on the
                # container filesystem while the destination may be a mounted
                # data disk. Copy into the destination directory first so the
                # final rename stays atomic without crossing filesystems.
                with (
                    temporary_path.open("rb") as source,
                    tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=pdf_path.parent,
                        prefix=f".{pdf_path.name}.",
                        suffix=".part",
                        delete=False,
                    ) as staging,
                ):
                    staging_path = Path(staging.name)
                    shutil.copyfileobj(source, staging, length=1024 * 1024)
                staging_path.replace(pdf_path)
                staging_path = None
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return duplicate
        except OSError as exc:
            raise DiscoveryStorageError(
                "Could not write the local download staging files."
            ) from exc
        finally:
            if staging_path is not None:
                staging_path.unlink(missing_ok=True)

    def _store_cos(
        self,
        temporary_path: Path,
        storage_key: str,
        metadata_key: str,
        metadata: dict[str, Any],
    ) -> bool:
        client = self._get_cos_client()
        bucket = self._require_cos_setting("cos_bucket")
        object_key = self._cos_object_key(storage_key)
        metadata_object_key = self._cos_object_key(metadata_key)
        try:
            duplicate = self._cos_object_exists(client, bucket, object_key)
            if not duplicate:
                client.upload_file(Bucket=bucket, Key=object_key, LocalFilePath=str(temporary_path))
            client.put_object(
                Bucket=bucket,
                Key=metadata_object_key,
                Body=json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"),
                ContentType="application/json; charset=utf-8",
            )
            return duplicate
        except DiscoveryStorageError:
            raise
        except Exception as exc:
            raise DiscoveryStorageError(
                "Could not upload the external PDF to Tencent COS."
            ) from exc

    def _get_cos_client(self):
        if self.cos_client is not None:
            return self.cos_client
        self._require_cos_setting("cos_bucket")
        region = self._require_cos_setting("cos_region")
        secret_id = self._require_cos_setting("cos_secret_id")
        secret_key = self._require_cos_setting("cos_secret_key")
        try:
            from qcloud_cos import CosConfig, CosS3Client

            config = CosConfig(
                Region=region,
                SecretId=secret_id,
                SecretKey=secret_key,
                Endpoint=self._sdk_endpoint(),
                Scheme="https",
            )
            self.cos_client = CosS3Client(config)
            return self.cos_client
        except DiscoveryStorageError:
            raise
        except Exception as exc:
            raise DiscoveryStorageError("Could not initialize the Tencent COS client.") from exc

    def _require_cos_setting(self, name: str) -> str:
        value = getattr(self.settings, name)
        if not value:
            raise DiscoveryStorageError(f"COS storage requires {name.upper()} to be configured.")
        return value

    def _cos_object_exists(self, client, bucket: str, object_key: str) -> bool:
        try:
            client.head_object(Bucket=bucket, Key=object_key)
            return True
        except Exception as exc:
            status_getter = getattr(exc, "get_status_code", None)
            status_code = status_getter() if callable(status_getter) else None
            if status_code == 404:
                return False
            raise DiscoveryStorageError("Could not check the Tencent COS object state.") from exc

    def _cos_object_key(self, storage_key: str) -> str:
        prefix = self.settings.cos_prefix.strip("/")
        return f"{prefix}/{storage_key}" if prefix else storage_key

    def _sdk_endpoint(self) -> str | None:
        endpoint = (self.settings.cos_endpoint or "").strip()
        if not endpoint:
            return None
        endpoint = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
        bucket_prefix = f"{self._require_cos_setting('cos_bucket')}."
        return endpoint.removeprefix(bucket_prefix)

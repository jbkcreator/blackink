"""PDF storage abstraction for the campaign audit report.

Current implementation: LocalPdfStore — writes bytes to a local directory
and returns a file:// URI. Drop-in S3PdfStore when AWS credentials are
configured (uncomment the class below and swap get_pdf_store()).

All implementations satisfy the PdfStore protocol:
    store = get_pdf_store()
    url   = store.put("company-abc/report.pdf", pdf_bytes)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class PdfStore(Protocol):
    def put(self, key: str, pdf_bytes: bytes) -> str:
        """Upload pdf_bytes under key; return a URL the email can reference."""
        ...


class LocalPdfStore:
    """Writes PDFs to a local directory. Suitable for dev/staging only."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def put(self, key: str, pdf_bytes: bytes) -> str:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pdf_bytes)
        logger.info("pdf_store: wrote %d bytes → %s", len(pdf_bytes), path)
        return path.resolve().as_uri()


# ── S3PdfStore (activate when AWS credentials are available) ──────────────
#
# class S3PdfStore:
#     def __init__(self, bucket: str, region: str) -> None:
#         import boto3
#         self._s3     = boto3.client("s3", region_name=region)
#         self._bucket = bucket
#
#     def put(self, key: str, pdf_bytes: bytes) -> str:
#         self._s3.put_object(
#             Bucket=self._bucket,
#             Key=key,
#             Body=pdf_bytes,
#             ContentType="application/pdf",
#         )
#         return f"https://{self._bucket}.s3.amazonaws.com/{key}"


def get_pdf_store() -> PdfStore:
    """Return the configured PdfStore.

    Reads PDF_LOCAL_DIR from settings (default: tmp/pdf).
    Swap for S3PdfStore once S3_BUCKET / AWS credentials are added to settings.
    """
    from config.settings import get_settings
    settings = get_settings()
    return LocalPdfStore(root=settings.pdf_local_dir)

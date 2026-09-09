"""Playwright website screenshot for the GIF generator.

Captures the prospect's homepage at 1200x675 (2x target) then
downscales to 600x338. Returns a PIL Image or None on failure.
"""
from __future__ import annotations

import io
import logging
from typing import Optional

from PIL import Image

TARGET_W  = 600
TARGET_H  = 338
CAPTURE_W = 1200   # 2x for quality, downscaled after
CAPTURE_H = 675

logger = logging.getLogger(__name__)


def capture(url: str, timeout_ms: int = 25_000) -> Optional[Image.Image]:
    """Screenshot url and return a 600x338 RGB PIL Image, or None on failure."""
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(
                    viewport={"width": CAPTURE_W, "height": CAPTURE_H},
                )
                # Try networkidle first; fall back to domcontentloaded for slow sites
                try:
                    page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                except Exception:
                    page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")

                raw = page.screenshot(type="png", clip={
                    "x": 0, "y": 0,
                    "width": CAPTURE_W, "height": CAPTURE_H,
                })
            finally:
                browser.close()

        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img = img.resize((TARGET_W, TARGET_H), Image.LANCZOS)
        logger.info("gif_generator: screenshot captured url=%s", url)
        return img

    except Exception as exc:
        logger.warning("gif_generator: screenshot failed url=%s: %s", url, exc)
        return None

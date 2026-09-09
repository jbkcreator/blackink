"""Unit tests for the GIF thumbnail generator (Subtask 2.2.2).

Tests run without a live DB, Playwright, or external services.
PIL is required; tests are skipped if it isn't installed.
"""
import io
import struct

import pytest

pytest.importorskip("PIL", reason="Pillow not installed")

from PIL import Image

from src.agents.ink.subagents.gif_generator.composer import (
    MAX_BYTES,
    TARGET_H,
    TARGET_W,
    compose_gif,
)


def _gif_dimensions(gif_bytes: bytes) -> tuple[int, int]:
    """Parse width/height from GIF header (bytes 6-9, little-endian)."""
    w = struct.unpack_from("<H", gif_bytes, 6)[0]
    h = struct.unpack_from("<H", gif_bytes, 8)[0]
    return w, h


def _make_screenshot() -> Image.Image:
    return Image.new("RGB", (TARGET_W, TARGET_H), color=(200, 210, 220))


class TestComposeGif:
    def test_returns_bytes_with_screenshot(self):
        img = _make_screenshot()
        result = compose_gif(
            screenshot=img,
            company_name="Test PM",
            latency_sec=3600,
            loss_est=45000,
        )
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_under_size_cap_with_screenshot(self):
        img = _make_screenshot()
        result = compose_gif(
            screenshot=img,
            company_name="Gulfshore Property Management",
            latency_sec=7200,
            loss_est=120000,
        )
        assert len(result) <= MAX_BYTES, f"GIF too large: {len(result)} bytes"

    def test_fallback_placeholder_when_no_screenshot(self):
        """compose_gif(screenshot=None) must succeed and return a valid GIF."""
        result = compose_gif(
            screenshot=None,
            company_name="No Screenshot Inc",
            latency_sec=None,
            loss_est=0,
        )
        assert isinstance(result, bytes)
        assert len(result) > 0
        assert result[:3] == b"GIF"

    def test_fallback_under_size_cap(self):
        result = compose_gif(
            screenshot=None,
            company_name="A" * 40,   # long name — tests truncation
            latency_sec=86400,
            loss_est=999999,
        )
        assert len(result) <= MAX_BYTES

    def test_gif_dimensions_correct(self):
        result = compose_gif(
            screenshot=_make_screenshot(),
            company_name="Dimension Test",
            latency_sec=300,
            loss_est=5000,
        )
        w, h = _gif_dimensions(result)
        assert w == TARGET_W, f"Expected width {TARGET_W}, got {w}"
        assert h == TARGET_H, f"Expected height {TARGET_H}, got {h}"

    def test_gif_magic_bytes(self):
        result = compose_gif(
            screenshot=None,
            company_name="Magic Test",
            latency_sec=None,
            loss_est=0,
        )
        assert result[:3] == b"GIF", "Result is not a GIF"

    def test_none_latency_handled(self):
        """None latency_sec (24h no-reply) must not crash the generator."""
        result = compose_gif(
            screenshot=None,
            company_name="No Reply PM",
            latency_sec=None,
            loss_est=50000,
        )
        assert isinstance(result, bytes)
        assert len(result) > 0

"""Overlay audit data on a screenshot and encode as a 2-frame animated GIF.

Frame 1 (500 ms)  -- raw screenshot (or grey placeholder if unavailable).
Frame 2 (2500 ms) -- screenshot + navy banner with response time + loss figure.

The two-frame animation catches the eye in email inbox previews while keeping
the file small. GIF palette is quantized to stay under 1.5 MB; falls back to
a single static frame as last resort.
"""
from __future__ import annotations

import io
import logging
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from src.agents.ink.subagents.pdf_generator.pdf_report import _fmt_latency, _status

TARGET_W  = 600
TARGET_H  = 338
BANNER_H  = 105
MAX_BYTES = 1_500_000   # 1.5 MB hard limit

# Brand palette (RGB) -- matches pdf_report.py
_NAVY  = (15,  23,  42)
_GOLD  = (234, 179,   8)
_WHITE = (255, 255, 255)
_RED   = (220,  38,  38)
_LIGHT = (200, 210, 225)
_GREY  = (100, 116, 139)

logger = logging.getLogger(__name__)


def compose_gif(
    screenshot:   Optional[Image.Image],
    company_name: str,
    latency_sec:  Optional[int],
    loss_est:     int,
) -> bytes:
    """Return animated GIF bytes (<=1.5 MB) from screenshot + audit data."""
    base = (screenshot or _placeholder()).convert("RGB").resize(
        (TARGET_W, TARGET_H), Image.LANCZOS
    )
    overlay = _draw_overlay(base.copy(), company_name, latency_sec, loss_est)

    gif_bytes = _encode([base, overlay], durations=[500, 2500])
    logger.info(
        "gif_generator: composed %d bytes  screenshot=%s",
        len(gif_bytes), screenshot is not None,
    )
    return gif_bytes


# ── internals ──────────────────────────────────────────────────────────────────

def _placeholder() -> Image.Image:
    img  = Image.new("RGB", (TARGET_W, TARGET_H), color=_GREY)
    draw = ImageDraw.Draw(img)
    draw.text(
        (TARGET_W // 2, (TARGET_H - BANNER_H) // 2),
        "Website preview unavailable",
        fill=_LIGHT, font=_font(15), anchor="mm",
    )
    return img


def _draw_overlay(
    img:          Image.Image,
    company_name: str,
    latency_sec:  Optional[int],
    loss_est:     int,
) -> Image.Image:
    draw     = ImageDraw.Draw(img)
    banner_y = TARGET_H - BANNER_H

    # Banner fill + gold separator line
    draw.rectangle([(0, banner_y), (TARGET_W, TARGET_H)], fill=_NAVY)
    draw.line([(0, banner_y), (TARGET_W, banner_y)], fill=_GOLD, width=2)

    # Row 1 -- brand label
    draw.text((10, banner_y + 6), "BLACKINK  |  RESPONSE TIME AUDIT",
              fill=_GOLD, font=_font(10))

    # Row 2 -- response time (left) + status badge (right)
    status_label, _ = _status(latency_sec)
    time_str = _fmt_latency(latency_sec)
    draw.text((10, banner_y + 24),
              f"Response Time: {time_str}",
              fill=_WHITE, font=_font(15))
    draw.text((TARGET_W - 10, banner_y + 24),
              f">> {status_label}",
              fill=_RED, font=_font(14), anchor="ra")

    # Row 3 -- loss estimate (large, red)
    draw.text((10, banner_y + 46),
              f"${loss_est:,}",
              fill=_RED, font=_font(28))

    # Row 4 -- sub-label (left) + company name (right)
    draw.text((10, banner_y + 83),
              "estimated annual revenue at risk",
              fill=_LIGHT, font=_font(11))
    draw.text((TARGET_W - 10, banner_y + 83),
              company_name[:38],
              fill=_LIGHT, font=_font(11), anchor="ra")

    return img


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Pillow < 10 -- load_default() takes no size argument
        return ImageFont.load_default()


def _encode(frames: list[Image.Image], durations: list[int]) -> bytes:
    """Quantize frames and encode GIF; shrink palette if over the size cap."""
    for n_colors in (256, 128, 64):
        quantized = [
            f.quantize(colors=n_colors, method=Image.Quantize.MEDIANCUT)
            for f in frames
        ]
        buf = io.BytesIO()
        quantized[0].save(
            buf,
            format="GIF",
            save_all=True,
            append_images=quantized[1:],
            loop=0,
            duration=durations,
            optimize=True,
        )
        data = buf.getvalue()
        if len(data) <= MAX_BYTES:
            if n_colors < 256:
                logger.info("gif_generator: reduced to %d colors -- %d bytes", n_colors, len(data))
            return data
        logger.warning("gif_generator: %d bytes at %d colors -- reducing", len(data), n_colors)

    # Last resort: single static frame (the overlay, not the blank first frame)
    logger.warning("gif_generator: falling back to single static frame")
    buf = io.BytesIO()
    frames[-1].quantize(colors=64).save(buf, format="GIF", optimize=True)
    return buf.getvalue()

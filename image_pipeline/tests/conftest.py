"""Synthetic image factories, fake network objects and HTML fixture loading.

Nothing here touches the network: the whole test suite runs offline.
"""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from config import PipelineConfig
from decisions import DecisionLog
from models import ModelKeys, PageCandidate, SourceSearchReport
from protocols import BinaryResponse, PageResponse

FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------------- #
# image factories
# --------------------------------------------------------------------------- #
def _canvas(width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", (width, height), (255, 255, 255))
    return image, ImageDraw.Draw(image)


def clean_product_image(width: int = 900, height: int = 900, colour: str = "#2f5fa8") -> Image.Image:
    """A white-background catalog photo: product in the middle, calm border."""
    image, draw = _canvas(width, height)
    margin_x, margin_y = int(width * 0.22), int(height * 0.18)
    draw.rounded_rectangle(
        (margin_x, margin_y, width - margin_x, height - margin_y), radius=24, fill=colour
    )
    return image


def busy_centre_image(width: int = 900, height: int = 900) -> Image.Image:
    """White corner patches but a busy frame: S7 must reject it.

    This is the trap the example script fell into: looking at the four corners only
    would accept it, so the whole border ring is checked instead.
    """
    rng = np.random.default_rng(7)
    noise = rng.integers(0, 255, size=(height, width, 3), dtype=np.uint8)
    image = Image.fromarray(noise, mode="RGB")
    draw = ImageDraw.Draw(image)
    patch = max(8, int(min(width, height) * 0.10))
    for left, top in ((0, 0), (width - patch, 0), (0, height - patch), (width - patch, height - patch)):
        draw.rectangle((left, top, left + patch, top + patch), fill=(255, 255, 255))
    return image


def wide_strip_image(width: int = 1000, height: int = 150) -> Image.Image:
    image, draw = _canvas(width, height)
    draw.rectangle((0, 0, width, height), fill=(220, 220, 220))
    return image


def tiny_image(size: int = 100) -> Image.Image:
    return clean_product_image(size, size)


def white_on_white_image(width: int = 800, height: int = 800) -> Image.Image:
    """A white product on a white background (isolation fallback territory)."""
    image, draw = _canvas(width, height)
    draw.rounded_rectangle((250, 250, 550, 550), radius=30, fill=(247, 247, 247),
                           outline=(235, 235, 235), width=3)
    return image


def second_angle_image(width: int = 900, height: int = 900) -> Image.Image:
    """A tall product, cleanly on white - structurally different from the first angle."""
    image, draw = _canvas(width, height)
    draw.rounded_rectangle((370, 170, 530, 740), radius=18, fill="#7a1f1f")
    draw.ellipse((390, 100, 510, 220), fill="#7a1f1f")
    return image


def third_angle_image(width: int = 900, height: int = 900) -> Image.Image:
    """A wide product (washing machine) with a round door: a third distinct angle."""
    image, draw = _canvas(width, height)
    draw.rounded_rectangle((160, 320, 740, 690), radius=20, fill="#e8d43c")
    draw.ellipse((350, 390, 550, 590), fill="#b9930c")
    return image


def qr_image(size: int = 420) -> Image.Image:
    """A real QR code (needs OpenCV's encoder); skipped if unavailable."""
    import cv2

    encoder = getattr(cv2, "QRCodeEncoder_create", None)
    if encoder is None:                                  # pragma: no cover - old cv2
        raise RuntimeError("this OpenCV build cannot encode QR codes")
    payload = encoder().encode("https://example.com/product")
    code = payload[1]
    code = cv2.resize(code, (size, size), interpolation=cv2.INTER_NEAREST)
    image = Image.fromarray(code, mode="L").convert("RGB")
    canvas, _ = _canvas(size + 160, size + 160)
    canvas.paste(image, (80, 80))
    return canvas


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
@dataclass
class FakePageFetcher:
    """Maps URL -> HTML.  Records what was requested (for S11 assertions)."""

    pages: dict[str, str]
    requested: list[str] = field(default_factory=list)

    def fetch_page(self, url: str) -> PageResponse:
        self.requested.append(url)
        if url not in self.pages:
            from fetch import PermanentAPIError

            raise PermanentAPIError(f"404 for {url}")
        return PageResponse(url=url, status_code=200, text=self.pages[url])


@dataclass
class FakeImageFetcher:
    """Maps URL -> bytes.  ``missing`` raises a permanent error."""

    images: dict[str, bytes]
    missing: set[str] = field(default_factory=set)
    requested: list[str] = field(default_factory=list)
    content_type: str = "image/png"
    #: url -> first bytes returned by :meth:`probe_image` (None = probe unavailable)
    probe_headers: dict[str, bytes] = field(default_factory=dict)
    probes: list[str] = field(default_factory=list)

    def fetch_image(self, url: str, referer: str) -> BinaryResponse:
        self.requested.append(url)
        if url in self.missing or url not in self.images:
            from fetch import PermanentAPIError

            raise PermanentAPIError(f"404 for {url}")
        return BinaryResponse(
            url=url, status_code=200, content=self.images[url], content_type=self.content_type
        )

    def probe_image(self, url: str, referer: str, max_bytes: int) -> BinaryResponse | None:
        """Header probe fake: only URLs in ``probe_headers`` answer, others return None."""
        self.probes.append(url)
        header = self.probe_headers.get(url)
        if header is None:
            return None
        return BinaryResponse(
            url=url, status_code=206, content=header, content_type=self.content_type
        )


@dataclass
class FakeSourceSearcher:
    """Fake fallback chain: returns prepared candidates and records the calls."""

    candidates: list[PageCandidate] = field(default_factory=list)
    report: SourceSearchReport = field(default_factory=SourceSearchReport)
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def find_candidates(
        self, *, title: str, brand: str | None, keys: ModelKeys
    ) -> tuple[list[PageCandidate], SourceSearchReport]:
        self.calls.append((title, brand))
        return list(self.candidates), self.report


@dataclass
class FakeImageHost:
    """Collects uploads.  ``permanent_after`` simulates a permanent host failure."""

    uploads: list[str] = field(default_factory=list)
    permanent_after: int | None = None
    counter: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def upload(self, data: bytes, filename: str) -> str:
        with self.lock:
            self.counter += 1
            if self.permanent_after is not None and self.counter > self.permanent_after:
                from fetch import PermanentAPIError

                raise PermanentAPIError("invalid API key (fake host)")
            self.uploads.append(filename)
            return f"https://img.example/{filename}"


# --------------------------------------------------------------------------- #
# config / keys / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def config(tmp_path: Path) -> PipelineConfig:
    assets = tmp_path / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    make_assets(assets)
    return PipelineConfig(
        assets_dir=assets,
        output_dir=tmp_path / "out",
        state_dir=tmp_path / "state",
        dry_run=True,
        workers=2,
        domain_min_interval_s=0.0,
        upload_min_interval_s=0.0,
    )


def make_assets(folder: Path) -> None:
    """Small but realistic templates: header band + footer, plus two banners."""
    template = Image.new("RGBA", (800, 800), (255, 255, 255, 255))
    draw = ImageDraw.Draw(template)
    draw.rectangle((0, 0, 800, 90), fill=(12, 78, 160, 255))
    draw.rectangle((0, 740, 800, 800), fill=(12, 78, 160, 255))
    template.save(folder / "DigiMall.png")
    template.save(folder / "AlfaMall.png")
    digi_banner = Image.new("RGB", (900, 300), (250, 250, 250))
    ImageDraw.Draw(digi_banner).rectangle((0, 0, 900, 60), fill=(200, 30, 60))
    digi_banner.save(folder / "digi_last.png")
    alfa_banner = Image.new("RGB", (900, 300), (245, 245, 252))
    ImageDraw.Draw(alfa_banner).rectangle((0, 0, 900, 60), fill=(20, 90, 170))
    alfa_banner.save(folder / "alfa_last.png")


@pytest.fixture()
def decisions(tmp_path: Path) -> DecisionLog:
    return DecisionLog(tmp_path / "out" / "decisions.jsonl")


def load_fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@pytest.fixture()
def html() -> object:
    return load_fixture



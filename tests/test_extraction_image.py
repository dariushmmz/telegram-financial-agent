from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from receipt_bot.extraction.diagnostics import DiagnosticCapture
from receipt_bot.extraction.errors import ImageLimitError, InvalidImageError, UnsupportedImageError
from receipt_bot.extraction.image import (
    ImageLimits,
    PreprocessOptions,
    prepare_image,
    sha256_bytes,
)


def image_bytes(
    image_format: str = "PNG",
    *,
    size: tuple[int, int] = (20, 10),
    mode: str = "RGB",
    color=None,
    exif=None,
) -> bytes:
    if color is None:
        color = (20, 80, 160, 180) if mode == "RGBA" else (20, 80, 160)
    image = Image.new(mode, size, color)
    # Add variation so conservative damage detection accepts modified images.
    highlight = (255, 255, 255, 255) if mode == "RGBA" else (255, 255, 255)
    for x in range(max(1, size[0] // 2)):
        for y in range(size[1]):
            image.putpixel((x, y), highlight)
    output = io.BytesIO()
    save_options = {"exif": exif} if exif is not None else {}
    image.save(output, format=image_format, **save_options)
    return output.getvalue()


def test_unmodified_jpeg_preserves_original_bytes_and_hash() -> None:
    data = image_bytes("JPEG")
    first = prepare_image(data, declared_mime_type="image/jpeg")
    second = prepare_image(data, declared_mime_type="image/jpeg")
    assert first.data == data
    assert first.mime_type == "image/jpeg"
    assert first.original_sha256 == sha256_bytes(data)
    assert first.sent_sha256 == second.sent_sha256
    assert first.record["steps"] == ["no changes needed: original bytes sent as-is"]


def test_exif_rotation_is_applied_and_sent_losslessly() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6
    data = image_bytes("JPEG", size=(20, 10), exif=exif)
    prepared = prepare_image(data)
    assert prepared.mime_type == "image/png"
    assert prepared.record["sent"]["size_px"] == [10, 20]
    assert any("exif_transpose" in step for step in prepared.record["steps"])


def test_transparency_flattens_to_white_and_uses_png() -> None:
    prepared = prepare_image(image_bytes(mode="RGBA"), declared_mime_type="image/png")
    assert prepared.mime_type == "image/png"
    assert "convert mode RGBA -> RGB" in prepared.record["steps"]


def test_large_side_is_downscaled_without_upscaling() -> None:
    data = image_bytes(size=(200, 100))
    prepared = prepare_image(data, limits=ImageLimits(max_side=50))
    assert prepared.record["sent"]["size_px"] == [50, 25]
    assert any(step.startswith("resize") for step in prepared.record["steps"])


def test_raw_mode_still_validates_then_preserves_original() -> None:
    data = image_bytes("WEBP")
    prepared = prepare_image(data, options=PreprocessOptions(raw=True))
    assert prepared.data == data
    assert prepared.mime_type == "image/webp"
    assert "validated original" in prepared.record["steps"][0]


def test_damaged_processed_image_falls_back_to_original_bytes() -> None:
    image = Image.new("RGBA", (20, 20), (255, 255, 255, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    data = output.getvalue()
    prepared = prepare_image(data)
    assert prepared.data == data
    assert prepared.sent_sha256 == prepared.original_sha256
    assert prepared.record["warnings"]
    assert "fallback" in prepared.record["steps"][-1]


@pytest.mark.parametrize(
    ("data", "limits", "error"),
    [
        (b"not-an-image", ImageLimits(), InvalidImageError),
        (image_bytes(), ImageLimits(max_bytes=10), ImageLimitError),
        (image_bytes(size=(20, 20)), ImageLimits(max_pixels=100), ImageLimitError),
        (image_bytes("GIF"), ImageLimits(), UnsupportedImageError),
    ],
)
def test_invalid_unsupported_and_oversized_inputs_are_rejected(data, limits, error) -> None:
    with pytest.raises(error):
        prepare_image(data, limits=limits)


def test_declared_mime_must_match_decoded_content() -> None:
    with pytest.raises(UnsupportedImageError):
        prepare_image(image_bytes("PNG"), declared_mime_type="image/jpeg")


def test_sensitive_diagnostics_are_opt_in_and_managed(tmp_path: Path) -> None:
    prepared = prepare_image(image_bytes())
    disabled = DiagnosticCapture(tmp_path / "diagnostics", enabled=False)
    assert disabled.capture_prepared(-1001, prepared) is None
    assert not (tmp_path / "diagnostics").exists()

    enabled = DiagnosticCapture(tmp_path / "diagnostics", enabled=True)
    keys = enabled.capture_prepared(-1001, prepared)
    assert keys is not None
    assert keys["image_key"].startswith("group_-1001/")
    assert enabled.storage.resolve_key(keys["image_key"]).read_bytes() == prepared.data
    assert enabled.storage.resolve_key(keys["metadata_key"]).is_file()

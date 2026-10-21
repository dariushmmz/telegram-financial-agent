from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

from receipt_bot.extraction.errors import ImageLimitError, InvalidImageError, UnsupportedImageError


SUPPORTED_FORMATS = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp")}
SUPPORTED_MODES = {"RGB", "L", "RGBA", "LA", "P", "CMYK", "1"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class ImageLimits:
    max_bytes: int = 15 * 1024 * 1024
    max_pixels: int = 40_000_000
    max_side: int = 4096

    def __post_init__(self) -> None:
        if self.max_bytes < 1 or self.max_pixels < 1 or self.max_side < 1:
            raise ValueError("image limits must be positive")


@dataclass(frozen=True, slots=True)
class PreprocessOptions:
    raw: bool = False
    enhance: bool = False
    grayscale: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class PreparedImage:
    data: bytes
    mime_type: str
    extension: str
    original_sha256: str
    sent_sha256: str
    record: dict[str, Any]


def _looks_damaged(image: Image.Image) -> str | None:
    gray = image if image.mode == "L" else image.convert("L")
    stat = ImageStat.Stat(gray)
    mean, std = stat.mean[0], stat.stddev[0]
    if std < 5:
        return f"almost uniform image (std={std:.1f})"
    if mean < 10:
        return f"almost black image (mean={mean:.1f})"
    return None


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    if has_alpha:
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _normalize_declared_mime(value: str | None) -> str | None:
    if value is None or value == "application/octet-stream":
        return None
    return "image/jpeg" if value.casefold() == "image/jpg" else value.casefold()


def prepare_image(
    data: bytes,
    *,
    declared_mime_type: str | None = None,
    limits: ImageLimits | None = None,
    options: PreprocessOptions | None = None,
) -> PreparedImage:
    """Validate and conservatively prepare untrusted image bytes for a provider."""
    limits = limits or ImageLimits()
    options = options or PreprocessOptions()
    if not data:
        raise InvalidImageError("image is empty")
    if len(data) > limits.max_bytes:
        raise ImageLimitError("image exceeds the configured byte limit")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                image_format = (probe.format or "").upper()
                width, height = probe.size
                if width < 1 or height < 1 or width * height > limits.max_pixels:
                    raise ImageLimitError("image exceeds the configured pixel limit")
                probe.verify()
    except Image.DecompressionBombWarning as exc:
        raise ImageLimitError("image exceeds Pillow's safe pixel limit") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise InvalidImageError("image cannot be decoded safely") from exc

    if image_format not in SUPPORTED_FORMATS:
        raise UnsupportedImageError("image format is not supported")
    actual_mime, original_extension = SUPPORTED_FORMATS[image_format]
    declared = _normalize_declared_mime(declared_mime_type)
    if declared is not None and declared != actual_mime:
        raise UnsupportedImageError("declared MIME type does not match decoded image content")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
            image.load()
    except (Image.DecompressionBombWarning, UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise InvalidImageError("image pixels cannot be loaded safely") from exc

    orientation = image.getexif().get(0x0112, 1)
    original_hash = sha256_bytes(data)
    record: dict[str, Any] = {
        "original": {
            "bytes": len(data),
            "sha256": original_hash,
            "format": image_format,
            "mode": image.mode,
            "size_px": [width, height],
            "exif_orientation": orientation,
        },
        "options": {
            "raw": options.raw,
            "enhance": options.enhance,
            "grayscale": options.grayscale,
            "max_side": limits.max_side,
        },
        "steps": [],
        "warnings": [],
    }

    def original_result(step: str) -> PreparedImage:
        record["steps"].append(step)
        record["sent"] = {
            "bytes": len(data),
            "sha256": original_hash,
            "mime_type": actual_mime,
            "size_px": [width, height],
        }
        return PreparedImage(data, actual_mime, original_extension, original_hash, original_hash, record)

    if options.raw:
        return original_result("raw mode: validated original bytes sent untouched")
    if image.mode not in SUPPORTED_MODES:
        raise UnsupportedImageError("image color mode is not supported")

    modified = False
    if orientation != 1:
        image = ImageOps.exif_transpose(image)
        record["steps"].append(f"exif_transpose: applied orientation {orientation}")
        modified = True

    if image.mode in ("RGBA", "LA", "P", "CMYK", "1"):
        before = image.mode
        image = _flatten_to_rgb(image) if before != "1" else image.convert("L")
        record["steps"].append(f"convert mode {before} -> {image.mode}")
        modified = True

    current_width, current_height = image.size
    if max(current_width, current_height) > limits.max_side:
        scale = limits.max_side / max(current_width, current_height)
        new_size = (round(current_width * scale), round(current_height * scale))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
        record["steps"].append(
            f"resize {current_width}x{current_height} -> {new_size[0]}x{new_size[1]}"
        )
        modified = True

    if options.grayscale:
        image = ImageOps.grayscale(image)
        record["steps"].append("grayscale")
        modified = True
    if options.enhance:
        image = ImageOps.autocontrast(image, cutoff=0)
        image = image.filter(ImageFilter.UnsharpMask(radius=1.0, percent=80, threshold=3))
        record["steps"].append("autocontrast + mild unsharp mask")
        modified = True

    if not modified:
        return original_result("no changes needed: original bytes sent as-is")

    damage = _looks_damaged(image)
    if damage:
        record["warnings"].append(f"processed image rejected: {damage}")
        return original_result("fallback: validated original bytes sent untouched")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    sent = buffer.getvalue()
    sent_hash = sha256_bytes(sent)
    record["sent"] = {
        "bytes": len(sent),
        "sha256": sent_hash,
        "mime_type": "image/png",
        "size_px": list(image.size),
    }
    return PreparedImage(sent, "image/png", "png", original_hash, sent_hash, record)

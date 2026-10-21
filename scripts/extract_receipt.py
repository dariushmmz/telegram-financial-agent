"""Compatibility CLI for provider-neutral Persian receipt extraction.

The production modules live under ``receipt_bot.extraction``. This wrapper keeps
the prototype command usable while changing sensitive debug capture from an
always-on default to an explicit ``--debug-dir`` option.
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import sys
from pathlib import Path

# Allow ``python scripts/extract_receipt.py`` from an uninstalled source checkout.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from google import genai

from receipt_bot.extraction.diagnostics import DiagnosticCapture
from receipt_bot.extraction.errors import ExtractionError
from receipt_bot.extraction.image import ImageLimits, PreprocessOptions, prepare_image
from receipt_bot.extraction.providers.common import RetryPolicy
from receipt_bot.extraction.providers.gemini import GeminiProvider
from receipt_bot.extraction.service import ExtractionService
from receipt_bot.extraction.validation import iban_is_valid

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MAX_SIDE_DEFAULT = 4096
log = logging.getLogger("receipt")


def preprocess_image(
    path: Path,
    raw: bool = False,
    enhance: bool = False,
    grayscale: bool = False,
    max_side: int = MAX_SIDE_DEFAULT,
) -> dict:
    """Backward-compatible path wrapper around the validated byte pipeline."""
    prepared = prepare_image(
        path.read_bytes(),
        declared_mime_type=mimetypes.guess_type(path.name)[0],
        limits=ImageLimits(max_side=max_side),
        options=PreprocessOptions(raw=raw, enhance=enhance, grayscale=grayscale),
    )
    return {
        "data": prepared.data,
        "mime": prepared.mime_type,
        "ext": prepared.extension,
        "record": prepared.record,
    }


def list_models() -> None:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is not set.")
    client = genai.Client(api_key=api_key)
    for model in client.models.list():
        actions = getattr(model, "supported_actions", None) or []
        if "generateContent" in actions:
            print(model.name.replace("models/", ""))


def extract_receipt(
    image_path: str,
    debug_dir: str | None = None,
    max_retries: int = 3,
    models: list[str] | None = None,
    **preprocess_options,
) -> dict:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ExtractionError("GEMINI_API_KEY is not set")

    path = Path(image_path)
    limits = ImageLimits(max_side=preprocess_options.pop("max_side", MAX_SIDE_DEFAULT))
    options = PreprocessOptions(**preprocess_options)
    provider = GeminiProvider(
        api_key=api_key,
        models=models or [DEFAULT_MODEL],
        retry_policy=RetryPolicy(max_attempts=max_retries),
    )
    service = ExtractionService(provider, limits=limits)
    outcome = service.extract(
        path.read_bytes(),
        declared_mime_type=mimetypes.guess_type(path.name)[0],
        options=options,
    )

    result = outcome.normalized.model_dump(mode="json")
    result["_provider"] = {
        "name": outcome.provider_result.provider,
        "model": outcome.provider_result.model,
        "request_id": outcome.provider_result.request_id,
    }
    result["_checks"] = {
        "iban_checksum_valid": (
            iban_is_valid(outcome.normalized.destination_iban)
            if outcome.normalized.destination_iban
            else None
        )
    }
    if debug_dir:
        prepared = prepare_image(
            path.read_bytes(),
            declared_mime_type=mimetypes.guess_type(path.name)[0],
            limits=limits,
            options=options,
        )
        result["_debug"] = DiagnosticCapture(Path(debug_dir), enabled=True).capture_prepared(0, prepared)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Persian receipt data with Gemini")
    parser.add_argument("image", nargs="?", help="Path to a JPG, PNG, or WebP receipt image")
    parser.add_argument("-o", "--output", help="Output JSON path (default: image path with .json)")
    parser.add_argument(
        "--debug-dir",
        help="Opt in to saving provider-bound image bytes and preprocessing metadata",
    )
    parser.add_argument("--model", help=f"Gemini model ID (default: {DEFAULT_MODEL})")
    parser.add_argument("--list-models", action="store_true", help="List models available to the API key")
    parser.add_argument("--raw", action="store_true", help="Send validated original bytes without preprocessing")
    parser.add_argument("--enhance", action="store_true", help="Apply mild autocontrast and sharpening")
    parser.add_argument("--grayscale", action="store_true", help="Convert the provider-bound image to grayscale")
    parser.add_argument("--max-side", type=int, default=MAX_SIDE_DEFAULT)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)

    if args.list_models:
        list_models()
        return
    if not args.image:
        parser.error("the image path is required")

    try:
        result = extract_receipt(
            args.image,
            debug_dir=args.debug_dir,
            max_retries=3,
            models=[args.model] if args.model else None,
            raw=args.raw,
            enhance=args.enhance,
            grayscale=args.grayscale,
            max_side=args.max_side,
        )
    except (ExtractionError, OSError) as exc:
        raise SystemExit(f"Extraction failed: {exc}") from None

    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    output_path = Path(args.output) if args.output else Path(args.image).with_suffix(".json")
    output_path.write_text(text, encoding="utf-8")
    log.info("Saved result: %s", output_path)


if __name__ == "__main__":
    main()

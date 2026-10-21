"""Opt-in live provider smoke test; never invoked by the automated suite."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from receipt_bot.extraction.errors import ExtractionError
from receipt_bot.extraction.providers.gemini import GeminiProvider
from receipt_bot.extraction.providers.openrouter import OpenRouterProvider
from receipt_bot.extraction.service import ExtractionService


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Make one explicitly authorized live extraction call and print a non-financial summary"
    )
    parser.add_argument("provider", choices=("gemini", "openrouter"))
    parser.add_argument("model", help="Current vision-capable provider model ID")
    parser.add_argument("image", type=Path, help="Synthetic or explicitly authorized JPG/PNG/WebP")
    args = parser.parse_args()

    if args.provider == "gemini":
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise SystemExit("GEMINI_API_KEY is required")
        provider = GeminiProvider(api_key=key, models=[args.model])
    else:
        key = os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise SystemExit("OPENROUTER_API_KEY is required")
        provider = OpenRouterProvider(api_key=key, model=args.model)

    try:
        outcome = ExtractionService(provider).extract(
            args.image.read_bytes(),
            declared_mime_type=mimetypes.guess_type(args.image.name)[0],
        )
    except ExtractionError as exc:
        safe_error = {
            "provider": args.provider,
            "model": args.model,
            "error_code": exc.code,
            "status_code": getattr(exc, "status_code", None),
            "retryable": exc.retryable,
        }
        print(json.dumps(safe_error, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(2) from None
    summary = {
        "provider": outcome.provider_result.provider,
        "model": outcome.provider_result.model,
        "request_id": outcome.provider_result.request_id,
        "requires_review": outcome.normalized.requires_review,
        "issue_codes": [issue.code for issue in outcome.normalized.validation_issues],
        "original_sha256": outcome.original_sha256,
        "sent_sha256": outcome.sent_sha256,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

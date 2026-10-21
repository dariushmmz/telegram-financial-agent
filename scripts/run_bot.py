"""Run the single-instance Telegram long-polling service."""
from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from receipt_bot.config import AppSettings
from receipt_bot.telegram.app import run_bot


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop_event.set)
        except NotImplementedError:
            signal.signal(
                signal_name,
                lambda _signum, _frame: loop.call_soon_threadsafe(stop_event.set),
            )
    await run_bot(AppSettings(), stop_event=stop_event)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

"""
Phase 4 notifier test script.

Usage: python scripts/test_notifier.py [telegram|email|desktop|all]
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

from app.config import get_settings
from app.notifier.telegram import TelegramNotifier
from app.notifier.email import EmailNotifier
from app.notifier.desktop import DesktopNotifier

TEST_MESSAGE = "Test alert: Ticket Tracker notifier working ✓"


async def test_notifier(name: str, notifier) -> None:
    if notifier.is_configured():
        await notifier.send(TEST_MESSAGE)
        print(f"Sent via {name}")
    else:
        print(f"Skipped {name} — not configured")


async def main() -> None:
    target = sys.argv[1].lower() if len(sys.argv) > 1 else "all"
    settings = get_settings()

    notifiers = {
        "telegram": TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        ),
        "email": EmailNotifier(
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            smtp_user=settings.smtp_user,
            smtp_password=settings.smtp_password,
            to_address=settings.alert_email_to,
        ),
        "desktop": DesktopNotifier(enabled=settings.desktop_notifications_enabled),
    }

    if target == "all":
        for name, notifier in notifiers.items():
            await test_notifier(name, notifier)
    elif target in notifiers:
        await test_notifier(target, notifiers[target])
    else:
        print(f"Unknown target '{target}'. Choose: telegram, email, desktop, all")
        sys.exit(1)


asyncio.run(main())

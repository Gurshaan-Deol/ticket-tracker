import sys, os, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.notifier.email import EmailNotifier
from app.notifier.desktop import DesktopNotifier
from app.config import get_settings

async def main():
    settings = get_settings()

    print("=== Email Notifier ===")
    email = EmailNotifier(
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_user=settings.smtp_user,
        smtp_password=settings.smtp_password,
        to_address=settings.alert_email_to,
    )
    print(f"Configured: {email.is_configured()}")
    if email.is_configured():
        await email.send("Test alert from Ticket Tracker — email working!")
        print("Email send() completed — check inbox")
    else:
        print("Email not configured — check .env")

    print()
    print("=== Desktop Notifier ===")
    desktop = DesktopNotifier(enabled=settings.desktop_notifications_enabled)
    print(f"Configured (enabled): {desktop.is_configured()}")
    if desktop.is_configured():
        await desktop.send("Test alert from Ticket Tracker — desktop working!")
        print("Desktop send() completed — did a notification pop up?")
    else:
        print("Desktop disabled — set DESKTOP_NOTIFICATIONS_ENABLED=true in .env to test")

asyncio.run(main())

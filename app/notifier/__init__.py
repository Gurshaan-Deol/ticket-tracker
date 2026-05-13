import logging

from app.config import get_settings
from app.notifier.base import BaseNotifier
from app.notifier.desktop import DesktopNotifier
from app.notifier.email import EmailNotifier
from app.notifier.telegram import TelegramNotifier

logger = logging.getLogger(__name__)


class NotifierManager:
    def __init__(self) -> None:
        settings = get_settings()
        self.notifiers: list[BaseNotifier] = []

        telegram = TelegramNotifier(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        if telegram.is_configured():
            self.notifiers.append(telegram)
            logger.info("Telegram notifier enabled")

        email = EmailNotifier(
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            smtp_user=settings.smtp_user,
            smtp_password=settings.smtp_password,
            to_address=settings.alert_email_to,
        )
        if email.is_configured():
            self.notifiers.append(email)
            logger.info("Email notifier enabled")

        desktop = DesktopNotifier(enabled=settings.desktop_notifications_enabled)
        if desktop.is_configured():
            self.notifiers.append(desktop)
            logger.info("Desktop notifier enabled")

        if not self.notifiers:
            logger.warning("No notification channels configured — alerts will only log")

    async def send_all(self, message: str) -> None:
        """Send message to all configured notifiers. Never raises."""
        for notifier in self.notifiers:
            await notifier.send(message)

    @property
    def channel_names(self) -> list[str]:
        """Return list of active channel names for logging."""
        return [type(n).__name__.replace("Notifier", "").lower() for n in self.notifiers]


_manager: NotifierManager | None = None


def get_notifier_manager() -> NotifierManager:
    global _manager
    if _manager is None:
        _manager = NotifierManager()
    return _manager

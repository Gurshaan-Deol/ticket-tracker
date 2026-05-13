import logging

from app.notifier.base import BaseNotifier

logger = logging.getLogger(__name__)


class DesktopNotifier(BaseNotifier):
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def is_configured(self) -> bool:
        return self.enabled

    async def send(self, message: str) -> None:
        try:
            from plyer import notification
            notification.notify(
                title="Ticket Price Alert",
                message=message[:256],
                app_name="Ticket Tracker",
                timeout=10,
            )
        except Exception as e:
            logger.error("Desktop notification failed: %s", e)

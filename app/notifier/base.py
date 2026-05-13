from abc import ABC, abstractmethod


class BaseNotifier(ABC):
    @abstractmethod
    async def send(self, message: str) -> None:
        """Send an alert message. Must never raise — log errors internally."""
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if this notifier has the credentials it needs to send."""
        pass

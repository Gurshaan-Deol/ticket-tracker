import asyncio
import logging
import smtplib
from email.mime.text import MIMEText

from app.notifier.base import BaseNotifier

logger = logging.getLogger(__name__)


class EmailNotifier(BaseNotifier):
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        to_address: str,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.to_address = to_address

    def is_configured(self) -> bool:
        return bool(self.smtp_user and self.smtp_password and self.to_address)

    async def send(self, message: str) -> None:
        def _send_sync():
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart

            msg = MIMEMultipart("alternative")
            msg["Subject"] = "Ticket Price Alert"
            msg["From"] = self.smtp_user
            msg["To"] = self.to_address
            msg.attach(MIMEText(message, "plain"))

            if self.smtp_port == 465:
                # SSL connection
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=15) as server:
                    server.login(self.smtp_user, self.smtp_password)
                    server.sendmail(self.smtp_user, self.to_address, msg.as_string())
            else:
                # STARTTLS connection (port 587)
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(self.smtp_user, self.smtp_password)
                    server.sendmail(self.smtp_user, self.to_address, msg.as_string())

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _send_sync)
        except Exception as e:
            logger.error("Email send failed: %s", e)

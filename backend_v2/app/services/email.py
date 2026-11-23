from __future__ import annotations

import smtplib
from email.message import EmailMessage

from app.core.errors import ServiceUnavailable
from app.core.settings import Settings, get_settings

PURPOSE_LABELS = {
    "signup": "verify your BidKaro email address",
    "login": "finish signing in to BidKaro",
}


class EmailService:
    """Gmail SMTP delivery for one-time codes.

    The app password is read from the environment only and is never logged,
    returned by an endpoint, or included in an email.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.smtp_username.strip()
            and self.settings.smtp_app_password.strip()
        )

    def send_code(self, *, to: str, code: str, purpose: str, expires_minutes: int) -> None:
        if not self.configured:
            raise ServiceUnavailable(
                "Email verification is not configured on the server. "
                "Set SMTP_USERNAME and SMTP_APP_PASSWORD."
            )

        action = PURPOSE_LABELS.get(purpose, "continue to BidKaro")
        message = EmailMessage()
        message["Subject"] = f"BidKaro verification code: {code}"
        message["From"] = (
            f"{self.settings.smtp_from_name} <{self.settings.smtp_username}>"
        )
        message["To"] = to
        message.set_content(
            "\n".join(
                [
                    f"Use this code to {action}:",
                    "",
                    code,
                    "",
                    f"The code expires in {expires_minutes} minutes. It can be used once.",
                    "If you did not request this, you can ignore this email.",
                    "",
                    "BidKaro",
                ]
            )
        )

        self._deliver(message, failure="verification email")

    def send_reminder(self, *, to: str, subject: str, body: str) -> None:
        if not self.configured:
            raise ServiceUnavailable(
                "Reminder email is not configured on the server. "
                "Set SMTP_USERNAME and SMTP_APP_PASSWORD."
            )

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = (
            f"{self.settings.smtp_from_name} <{self.settings.smtp_username}>"
        )
        message["To"] = to
        message.set_content(body)
        self._deliver(message, failure="reminder email")

    def _deliver(self, message: EmailMessage, *, failure: str) -> None:
        try:
            with smtplib.SMTP_SSL(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=max(self.settings.smtp_timeout_seconds, 5),
            ) as smtp:
                smtp.login(self.settings.smtp_username, self.settings.smtp_app_password)
                smtp.send_message(message)
        except ServiceUnavailable:
            raise
        except Exception as exc:
            raise ServiceUnavailable(
                f"We could not send the {failure}. Check the SMTP settings and try again."
            ) from exc

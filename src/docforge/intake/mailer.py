"""Sending intake's plain replies (V2 P5), by SMTP or not at all.

`SmtpMailer` takes `smtp://user:password@host:587` (STARTTLS) or `smtps://...:465` (TLS from
the start): Postmark's SMTP server in this deployment, or any mailbox's. Every reply carries
`Auto-Submitted: auto-replied` (RFC 3834), so a well-behaved mail system never answers it and
two automatic systems cannot reply to each other for ever.
"""

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reply:
    to: str
    subject: str
    body: str
    in_reply_to: str | None = None


class Mailer(Protocol):
    def send(self, reply: Reply) -> None: ...


class NoMailer:
    """No SMTP configured: replies are not sent (and the log says so, without content)."""

    def send(self, reply: Reply) -> None:
        logger.info("intake reply not sent: no SMTP_URL configured")


class SmtpMailer:
    def __init__(self, url: str, sender: str, *, timeout: float = 20.0) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("smtp", "smtps") or not parts.hostname:
            raise ValueError("SMTP_URL is smtp://user:password@host:port or smtps://...")
        self._tls = parts.scheme == "smtps"
        self._host = parts.hostname
        self._port = parts.port or (465 if self._tls else 587)
        self._user = unquote(parts.username) if parts.username else None
        self._password = unquote(parts.password) if parts.password else None
        self._sender = sender
        self._timeout = timeout

    def send(self, reply: Reply) -> None:
        message = EmailMessage()
        message["From"] = self._sender
        message["To"] = reply.to
        message["Subject"] = reply.subject
        message["Auto-Submitted"] = "auto-replied"
        if reply.in_reply_to:
            message["In-Reply-To"] = reply.in_reply_to
            message["References"] = reply.in_reply_to
        message.set_content(reply.body)
        context = ssl.create_default_context()
        if self._tls:
            with smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout, context=context
            ) as smtp:
                self._login_and_send(smtp, message)
        else:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
                smtp.starttls(context=context)
                self._login_and_send(smtp, message)

    def _login_and_send(self, smtp: smtplib.SMTP, message: EmailMessage) -> None:
        if self._user and self._password:
            smtp.login(self._user, self._password)
        smtp.send_message(message)

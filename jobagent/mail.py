"""SMTP delivery shared by all emails: SMTP_* first, then the Brevo relay (BREVO_SMTP_*) as fallback."""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

log = logging.getLogger(__name__)


def build(
    subject: str, sender: str, to: str, text: str, html: str, reply_to: str = "", headers: dict | None = None
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    if reply_to:
        msg["Reply-To"] = reply_to
    domain = sender.rsplit("@", 1)[-1].strip("> ")
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid(domain=domain)
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def _servers():
    for prefix, key in (("SMTP", "SMTP_PASS"), ("BREVO_SMTP", "BREVO_SMTP_KEY")):
        host = os.environ.get(f"{prefix}_HOST")
        if host:
            yield (
                prefix,
                host,
                int(os.environ.get(f"{prefix}_PORT", 587)),
                os.environ.get(f"{prefix}_USER", ""),
                os.environ.get(key, ""),
            )


def deliver(msg: EmailMessage) -> str:
    """Send via the first server that works; returns its name. Raises if all fail."""
    errors = []
    for name, host, port, user, password in _servers():
        try:
            ctx = ssl.create_default_context()
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=60)
            else:
                server = smtplib.SMTP(host, port, timeout=60)
                server.starttls(context=ctx)
            with server:
                if user:
                    server.login(user, password)
                server.send_message(msg)
            return name
        except (smtplib.SMTPException, OSError) as e:
            log.warning("sending via %s failed: %s", name, e.__class__.__name__)
            errors.append(f"{name}: {e.__class__.__name__}")
    raise RuntimeError("all mail servers failed: " + "; ".join(errors) if errors else "no SMTP server configured")

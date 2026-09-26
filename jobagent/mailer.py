"""Digest email for precision profiles."""

import os

from . import mail


def send(subject: str, html_body: str, text_body: str, to: str) -> str:
    sender = os.environ.get("JOBAGENT_SENDER", to)
    msg = mail.build(
        subject,
        f"WebZap Jobs <{sender}>",
        to,
        text_body,
        html_body,
        reply_to=os.environ.get("JOBAGENT_REPLY_TO", sender),
    )
    return mail.deliver(msg)

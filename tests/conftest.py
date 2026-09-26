"""Test setup: the web modules read their configuration at import time, so set a throw-away environment first."""

import base64
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="webzap-jobs-test-")
# A stand-in for an instance's legal pages: a binding Spanish privacy policy, its English translation, no imprint.
_legal = os.path.join(_tmp, "legal")
os.makedirs(_legal)
for _name, _text in [("privacy.es", "Texto vinculante"), ("privacy.en", "English translation")]:
    with open(os.path.join(_legal, f"{_name}.html"), "w") as _f:
        _f.write('{% extends "legal/_layout.html" %}{% block heading %}P{% endblock %}{% block legal %}' + _text)
        _f.write("{% endblock %}")
os.environ.update(
    {
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "HASH_PEPPER": "test-pepper",
        "DB_PATH": os.path.join(_tmp, "test.sqlite3"),
        "BASE_URL": "https://jobs.example.com",
        "CONTACT_EMAIL": "contact@example.com",
        "MAIL_FROM": "WebZap Jobs <noreply@example.com>",
        "LEGAL_DIR": _legal,
        "LEGAL_BINDING_LANG": "es",
        "DIGEST_CONFIG": os.path.join(os.path.dirname(__file__), "..", "digest.toml"),
    }
)

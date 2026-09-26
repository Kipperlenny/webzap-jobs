"""Every locale has exactly the English keys and placeholders; every key the templates use exists."""

import re
import string
from pathlib import Path

import pytest

import i18n

WEB = Path(__file__).resolve().parent.parent / "web"


def placeholders(value) -> set[str]:
    values = value if isinstance(value, list) else [value]
    return {f for v in values for _, f, _, _ in string.Formatter().parse(v) if f}


@pytest.mark.parametrize("lang", [lang for lang in i18n.LANGS if lang != i18n.DEFAULT])
def test_locale_matches_english(lang):
    en, other = i18n.STRINGS["en"], i18n.STRINGS[lang]
    missing, extra = sorted(set(en) - set(other)), sorted(set(other) - set(en))
    assert not missing and not extra, f"{len(missing)} missing, e.g. {missing[:5]}; extra: {extra[:5]}"
    for key, value in en.items():
        assert placeholders(other[key]) == placeholders(value), key
        assert isinstance(other[key], list) == isinstance(value, list), key


def test_every_key_used_in_code_and_templates_exists():
    used = set()
    for f in [*WEB.rglob("*.html"), *WEB.rglob("*.txt"), *WEB.glob("*.py")]:
        used |= set(re.findall(r"""_\(\s*["']([a-z_]+(?:\.[a-z_]+)+)["'](?!\s*~)""", f.read_text()))
    assert used, "found no translation keys – the pattern is broken"
    assert not used - set(i18n.STRINGS["en"]), used - set(i18n.STRINGS["en"])


def test_negotiate():
    assert i18n.negotiate("de-DE,de;q=0.9,en;q=0.8") == "de"
    assert i18n.negotiate("ja,fr;q=0.5,es;q=0.7") == "es"
    assert i18n.negotiate("") == i18n.negotiate(None) == i18n.negotiate("x;q=abc") == "en"
    assert i18n.negotiate("de;q=0") == "en"


def test_markup_escapes_arguments_but_plain_text_does_not():
    assert "&lt;b&gt;" in i18n.gettext("en", "email.digest.why_role", role="<b>")
    assert "<b>" in i18n.gettext("en", "email.digest.why_role", markup=False, role="<b>")

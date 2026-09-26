import extract


def test_validate_fills_defaults_and_clamps_clarity():
    d = extract.validate({"summary": "You want X.", "target_roles": None, "clarity": 9, "relocation": None})
    assert d["target_roles"] == []
    assert d["clarity"] == 5
    assert d["relocation"] is None
    assert d["intro"] == ""


def test_needs_help_for_vague_profiles_only():
    vague = extract.validate({"summary": "s", "clarity": 2, "target_roles": ["Dog Walker"], "questions": ["Where?"]})
    clear = extract.validate({"summary": "s", "clarity": 5, "target_roles": ["Nurse"]})
    assert extract.needs_help(vague)
    assert not extract.needs_help(clear)
    assert not extract.needs_help(None)


def test_prompt_includes_feedback_only_when_given():
    assert "Feedback on jobs" not in extract.build_user_prompt("I want to be a nurse.")
    p = extract.build_user_prompt(
        "I want to be a nurse.", [{"title": "Nurse", "company": "A", "vote": "down", "reasons": "too junior"}]
    )
    assert "👎 Nurse (A) – too junior" in p

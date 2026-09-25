"""Guards against stale prototype branding and unreviewed contact links."""

from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

BANNED_SUBSTRINGS = [
    "Salary Bot",
    "t.me/",
]


def test_templates_have_no_stale_branding_or_placeholder_links():
    offenders = []
    for path in TEMPLATES_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        for needle in BANNED_SUBSTRINGS:
            if needle in text:
                offenders.append(f"{path.relative_to(TEMPLATES_DIR)}: {needle!r}")

    assert not offenders, "Stale branding/placeholder links found:\n" + "\n".join(offenders)

"""The controlled browser fixture remains local and covers required primitives."""

from html.parser import HTMLParser
from pathlib import Path


class Controls(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.controls: dict[str, dict[str, str | None]] = {}
        self.external_resources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"input", "textarea", "select"} and attributes.get("name"):
            self.controls[str(attributes["name"])] = {"tag": tag, **attributes}
        if tag in {"script", "iframe", "img", "link"}:
            for key in ("src", "href"):
                if attributes.get(key):
                    self.external_resources.append(str(attributes[key]))


def test_browser_fixture_contains_widgets_without_external_resources() -> None:
    fixture = Path("tests/fixtures/application_agent/controlled_application.html")
    parser = Controls()
    parser.feed(fixture.read_text(encoding="utf-8"))
    assert parser.external_resources == []
    assert parser.controls["resume"]["type"] == "file"
    assert parser.controls["sponsorship"]["type"] == "radio"
    assert parser.controls["acknowledge"]["type"] == "checkbox"
    assert "multiple" in parser.controls["skills"]
    assert parser.controls["why_role"]["tag"] == "textarea"
    assert parser.controls["tracking"]["type"] == "hidden"
    assert "required" in parser.controls["first_name"]

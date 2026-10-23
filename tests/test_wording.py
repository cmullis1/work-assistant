"""The owner calls the people on the team "Techs" (Team activity design, Task 8). No owner-visible label says
"agent": the Zendesk role name, the scorecard and rule text, the reviewer prompts, addresses, API names and
JSON keys keep it."""
from __future__ import annotations

import ast
import re
from html.parser import HTMLParser

from css_rules import STATIC

AGENT = re.compile(r"\bagents?\b", re.IGNORECASE)

# Every double- or single-quoted string, or a comment (so a comment's quotes are skipped), left to right.
JS_TOKEN = re.compile(r'''"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|/\*.*?\*/|//[^\n]*''', re.DOTALL)

# The only owner-visible strings that may say "agent": Zendesk's own role name, shown as Zendesk reports it. None is
# left in app.js: adding a tech by name shows the server's "No Zendesk agent at <email>" (T-107).
ALLOWED: set[str] = set()


def js_strings(source: str) -> list[str]:
    """The text of every string literal in `source`, comments left out."""
    return [token[1:-1] for token in JS_TOKEN.findall(source) if token[0] in "\"'"]


def is_machine_name(text: str) -> bool:
    """An address, API path, JSON key or class name: lowercase, with no space (`agents`, `#/agents/`, `trow-agent`).
    Owner-visible words are capitalised ("Agent"), have a space ("All agents"), or are an email ("agent@...")."""
    return text == text.lower() and " " not in text and "@" not in text


def test_no_agent_in_owner_visible_strings():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    found = [s for s in js_strings(js) if AGENT.search(s) and not is_machine_name(s)]
    assert [s for s in found if s not in ALLOWED] == []
    assert set(ALLOWED) <= set(found), "an allowlisted string is gone: drop it from ALLOWED"


class _VisibleText(HTMLParser):
    """The text of index.html and the attributes the owner reads or hears."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.texts.append(data.strip())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.texts += [value for name, value in attrs if name in ("aria-label", "placeholder", "title", "alt") and value]


def test_no_agent_in_index_html():
    parser = _VisibleText()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    assert parser.texts and [t for t in parser.texts if AGENT.search(t)] == []


def test_scanner_sees_a_new_agent_label():
    """The scan is exact: a label added later, or a changed allowlisted one, fails it."""
    sample = 'h("th", { text: "Agent" }); h("p", null, \'Add an agent\'); x = "#/agents/"; y = "agent@company.com"; // "agents" here\n'
    found = [s for s in js_strings(sample) if AGENT.search(s) and not is_machine_name(s)]
    assert found == ["Agent", "Add an agent", "agent@company.com"]


SICK_DAY_PY_FILES = ("activity/trends.py", "activity/rules.py", "activity/store.py", "web/app.py", "web/queries.py")


def test_no_missed_shift_in_owner_visible_text():
    """A scheduled day with no sign-in and no action is a "Sick day" to the owner (T-071). The day state and flag
    kind `missed`, the measure key `missed_shifts` and Call QA's "missed" checks keep their names; no owner-visible
    string says "Missed shift" in any case."""
    pattern = re.compile(r"missed\s+shifts?", re.IGNORECASE)
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    found = [s for s in js_strings(js) if pattern.search(s)]
    parser = _VisibleText()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    found += [t for t in parser.texts if pattern.search(t)]
    package = STATIC.parent.parent
    for name in SICK_DAY_PY_FILES:
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
                      and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant)}
        found += [f"{name}: {node.value}" for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and id(node) not in docstrings and pattern.search(node.value)]
    assert found == []
    assert "Sick day" in js_strings(js), "the flag label"

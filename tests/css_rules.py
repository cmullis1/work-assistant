"""Shared helpers for tests that read the static front end: CSS rules by selector and property, regardless
of formatting, and the class names app.js gives an element."""
from __future__ import annotations

import re
from pathlib import Path

from work_assistant.web import app as webapp

STATIC = Path(webapp.__file__).parent / "static"


def app_css() -> str:
    """app.css with its comments removed."""
    return re.sub(r"/\*.*?\*/", "", (STATIC / "app.css").read_text(encoding="utf-8"), flags=re.S)


def rules(css: str, selector: str) -> list[str]:
    """Bodies of every rule whose selector list names `selector` exactly (media blocks included)."""
    found = []
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if selector in (" ".join(s.split()) for s in selectors.split(",")):
            found.append(body)
    return found


def media_block(css: str, query: str) -> str:
    """The text inside every `@media <query> { ... }` block, joined."""
    blocks = []
    for match in re.finditer(re.escape("@media " + query) + r"\s*\{", css):
        depth, i = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        blocks.append(css[match.end():i - 1])
    return "\n".join(blocks)


def top_level(css: str) -> str:
    """The CSS outside every @media block: the rules that apply at every width."""
    out, i = [], 0
    for match in re.finditer(r"@media[^{]*\{", css):
        if match.start() < i:
            continue
        out.append(css[i:match.start()])
        depth, i = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
    out.append(css[i:])
    return "".join(out)


def values(css: str, selector: str, prop: str) -> list[str]:
    """Every value `selector`'s rules give `prop`, whitespace normalized, in source order."""
    found = []
    for body in rules(css, selector):
        for declaration in body.split(";"):
            name, _, value = declaration.partition(":")
            if name.strip() == prop:
                found.append(" ".join(value.split()))
    return found


def last_value(css: str, selector: str, prop: str) -> str | None:
    """The value that wins for `prop` among `selector`'s own rules (the last one), or None."""
    found = values(css, selector, prop)
    return found[-1] if found else None


def tokens(css: str) -> dict[str, str]:
    """The custom properties `:root` defines, by name without the leading dashes ("page-max": "1760px")."""
    found = {}
    for body in rules(top_level(css), ":root"):
        for declaration in body.split(";"):
            name, _, value = declaration.partition(":")
            if name.strip().startswith("--"):
                found[name.strip()[2:]] = " ".join(value.split())
    return found


def px(css: str, value: str) -> float:
    """A length in px, written as "440px", "0", or "var(--token)" naming one in `:root`."""
    match = re.fullmatch(r"var\(--([\w-]+)\)", value.strip())
    if match:
        value = tokens(css)[match.group(1)]
    if value.strip() == "0":
        return 0.0
    assert value.endswith("px"), value
    return float(value[:-2])


def class_sets(js: str, tag: str) -> list[set[str]]:
    """The class names of every `h("tag", { class: ... })` in `js`. A class written as an expression (say a
    ternary) contributes every name in each of its string literals."""
    found = []
    for match in re.finditer(r'h\(\s*"' + re.escape(tag) + r'"\s*,\s*\{\s*class:\s*([^,}]*)', js):
        names = set()
        for literal in re.findall(r'"([^"]*)"', match.group(1)):
            names.update(literal.split())
        found.append(names)
    return found


def has_element(js: str, tag: str, *classes: str) -> bool:
    """Whether `js` builds a `tag` element carrying every one of `classes`."""
    return any(set(classes) <= names for names in class_sets(js, tag))

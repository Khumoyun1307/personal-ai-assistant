"""Publish docs/legal/privacy-policy.md to telegra.ph (FR-PRV-01).

Outside `src/` and never imported by it (AGENTS §5). The repository file is the
source of truth: the published text is the section between the first two
`---` lines, converted to Telegraph nodes (headings, paragraphs, bullet lists,
links).

    docker compose -f compose.dev.yml run --rm app python tools/legal/publish_policy.py

The first run creates a Telegraph account and the page; later runs edit the
same page. The Telegraph access token is a secret: it is stored in the
git-ignored `var/legal/telegraph.json` and never printed. Only the page URL is
printed.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "docs" / "legal" / "privacy-policy.md"
STATE = ROOT / "var" / "legal" / "telegraph.json"
API = "https://api.telegra.ph"
AUTHOR = "Personal assistant"
_URL = re.compile(r"(https?://\S+)")


def published_section(markdown: str) -> list[str]:
    lines = markdown.splitlines()
    marks = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(marks) < 2:
        raise SystemExit("policy file must mark the published text with two --- lines")
    return lines[marks[0] + 1 : marks[1]]


def inline(text: str) -> list[Any]:
    """Plain text with URLs turned into links."""
    parts: list[Any] = []
    for piece in _URL.split(text):
        if not piece:
            continue
        if _URL.fullmatch(piece):
            parts.append({"tag": "a", "attrs": {"href": piece}, "children": [piece]})
        else:
            parts.append(piece)
    return parts


def to_nodes(lines: list[str]) -> tuple[str, list[Any]]:
    title = ""
    nodes: list[Any] = []
    bullets: list[Any] = []
    paragraph: list[str] = []

    def flush() -> None:
        nonlocal bullets, paragraph
        if paragraph:
            nodes.append({"tag": "p", "children": inline(" ".join(paragraph))})
            paragraph = []
        if bullets:
            nodes.append({"tag": "ul", "children": bullets})
            bullets = []

    for raw in lines:
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("## "):
            flush()
            heading = line[3:].strip()
            if not title:
                title = heading
            else:
                nodes.append({"tag": "h3", "children": [heading]})
        elif line.startswith("- "):
            if paragraph:
                flush()
            bullets.append({"tag": "li", "children": inline(line[2:])})
        else:
            paragraph.append(line)
    flush()
    return title, nodes


def call(method: str, params: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(f"{API}/{method}", data=data, method="POST")  # noqa: S310
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed https host
        body = json.loads(response.read())
    if not body.get("ok"):
        raise SystemExit(f"telegraph {method} failed: {body.get('error')}")
    return dict(body["result"])


def main() -> None:
    title, nodes = to_nodes(published_section(POLICY.read_text(encoding="utf-8")))
    state: dict[str, str] = json.loads(STATE.read_text()) if STATE.exists() else {}
    if "access_token" not in state:
        account = call("createAccount", {"short_name": "policy", "author_name": AUTHOR})
        state["access_token"] = account["access_token"]
    params = {
        "access_token": state["access_token"],
        "title": title,
        "author_name": AUTHOR,
        "content": json.dumps(nodes, ensure_ascii=False),
        "return_content": "false",
    }
    if "path" in state:
        page = call(f"editPage/{state['path']}", params)
    else:
        page = call("createPage", params)
    state["path"] = page["path"]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))
    print(page["url"])


if __name__ == "__main__":
    main()

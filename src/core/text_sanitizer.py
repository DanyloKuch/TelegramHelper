"""Привод произвольного HTML к whitelist'у Telegram parse_mode=HTML.
br/p превращаются в переносы, всё остальное вне whitelist'а вырезается."""
from __future__ import annotations

import re
from html.parser import HTMLParser


_KEEP_TAGS = {"b", "strong", "i", "em", "u", "s", "strike", "code", "pre",
              "a", "tg-spoiler", "blockquote"}
_NORMALIZE = {"strong": "b", "em": "i", "strike": "s"}
_BLOCK_TO_NEWLINE = {"br", "p", "div", "li"}


class _Cleaner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag in _BLOCK_TO_NEWLINE:
            if self.parts and not self.parts[-1].endswith("\n"):
                self.parts.append("\n")
            return
        if tag not in _KEEP_TAGS:
            return
        norm = _NORMALIZE.get(tag, tag)
        if norm == "a":
            href = ""
            for k, v in attrs:
                if k.lower() == "href" and v:
                    href = v
                    break
            if href:
                href = href.replace('"', "&quot;")
                self.parts.append(f'<a href="{href}">')
            else:
                self.parts.append("<a>")
        else:
            self.parts.append(f"<{norm}>")

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in _BLOCK_TO_NEWLINE:
            return
        if tag not in _KEEP_TAGS:
            return
        norm = _NORMALIZE.get(tag, tag)
        self.parts.append(f"</{norm}>")

    def handle_data(self, data: str):
        # Якщо сюди дійшов буквальний "<"/">" — це НЕ розпізнаний тег (напр. Python
        # html.parser не вважає тегом "<Тема>", бо після "<" йде кирилиця, а не a-zA-Z), і
        # HTMLParser віддає його як звичайний текст. Без екранування Telegram потім падає з
        # "can't parse entities: Unsupported start tag".
        self.parts.append(data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def handle_entityref(self, name: str):
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str):
        self.parts.append(f"&#{name};")

    def result(self) -> str:
        return "".join(self.parts)


_FENCED = re.compile(r"^```(\w+)?\n(.*?)\n```$", re.DOTALL)


def sanitize_html(text: str | None) -> str:
    if not text:
        return ""
    raw = text.strip()
    # markdown ```fence``` → <pre>
    m = _FENCED.match(raw)
    if m:
        body = m.group(2)
        return "<pre>" + _escape(body) + "</pre>"

    cleaner = _Cleaner()
    try:
        cleaner.feed(raw)
        cleaner.close()
        out = cleaner.result()
    except Exception:
        out = _escape(raw)

    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

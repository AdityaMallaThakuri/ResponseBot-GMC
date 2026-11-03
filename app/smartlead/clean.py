import re
from html.parser import HTMLParser

_BLOCK_TAGS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP_TAGS = {"style", "script", "head", "blockquote"}
_VOID_TAGS = {"br", "hr", "img", "meta", "link", "input"}

_QUOTE_HEADERS = [
    re.compile(r"^On .{5,200}wrote:\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*(Original Message|Forwarded message)", re.IGNORECASE),
    re.compile(r"^_{5,}\s*$"),
]
_OUTLOOK_FROM = re.compile(r"^From:\s.+")
_OUTLOOK_NEXT = re.compile(r"^(Sent|Date|To):")
_WROTE_LINE = re.compile(r".*wrote:\s*$")
_SIGNATURE_MARKERS = [
    re.compile(r"^--\s*$"),
    re.compile(r"^Sent from my (iPhone|iPad|Android|Galaxy|mobile)", re.IGNORECASE),
    re.compile(r"^Get Outlook for ", re.IGNORECASE),
    re.compile(r"^Sent (via|with) ", re.IGNORECASE),
]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_TAGS:  # no end tag, so never track on the skip stack
            if not self._skip_stack and tag == "br":
                self.parts.append("\n")
            return
        classes = dict(attrs).get("class") or ""
        if self._skip_stack or tag in _SKIP_TAGS or "gmail_quote" in classes:
            self._skip_stack.append(tag)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._skip_stack and tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_stack:
            self.parts.append(data)


def strip_html(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


def _is_quote_header(line: str, next_line: str) -> bool:
    if any(p.match(line) for p in _QUOTE_HEADERS):
        return True
    # Outlook header block: "From: ..." followed by Sent:/Date:/To:
    if _OUTLOOK_FROM.match(line):
        return bool(_OUTLOOK_NEXT.match(next_line))
    # "On Mon, Jan 1, 2024 at 9:00 AM Name <x@y.com>" wrapped over two lines
    return line.startswith("On ") and bool(_WROTE_LINE.match(next_line))


def clean_body(body: str) -> str:
    """Return only the new text of a message: no HTML, quoted history or signature."""
    text = strip_html(body) if re.search(r"<[a-zA-Z/][^>]*>", body) else body
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [line.rstrip() for line in text.split("\n")]
    kept: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if _is_quote_header(stripped, next_line) or any(
            p.match(stripped) for p in _SIGNATURE_MARKERS
        ):
            break
        if stripped.startswith(">"):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()

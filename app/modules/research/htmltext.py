"""SEC filing HTML -> plain text blocks + structured tables (stdlib only, no HTML library dependency).

* Hidden iXBRL header, scripts, styles and `display:none` blocks are dropped.
* Tables that look like data (>= 2 rows, >= 2 columns, mostly numeric cells) are returned as JSON-able
  `Table`s and are *not* part of the prose (SPEC 7.5 step 4: keep tables as structured data). Layout tables
  (headings, single-cell wrappers, mostly text) are flattened to prose so Item headings are not lost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "hr"}
)
SKIP_TAGS = frozenset({"script", "style", "head", "ix:header", "noscript"})
VOID_TAGS = frozenset({"br", "hr", "img", "meta", "link", "input", "col", "area", "base", "wbr"})
_WS = re.compile(r"[ \t\r\f\v\u00a0\u200b]+")
_HEADING_CELL = re.compile(r"^(?:part|item)\s+[\divx]+", re.IGNORECASE)
_NUMERIC_CELL = re.compile(r"^[\s$()%,.\-\u2013\u2014\d]+$")


TABLE_MARK = re.compile(r"\u27e6T(\d+)\u27e7")


def table_marker(idx: int) -> str:
    return f"\u27e6T{idx}\u27e7"


@dataclass(slots=True)
class Table:
    rows: list[list[str]]
    idx: int  # a marker line "⟦T<idx>⟧" in the prose shows where the table sat (used to assign an Item)

    def as_json(self) -> list[list[str]]:
        return self.rows


@dataclass(slots=True)
class ParsedDocument:
    text: str
    tables: list[Table] = field(default_factory=list)


def _clean(s: str) -> str:
    return _WS.sub(" ", s).strip()


def is_data_table(rows: list[list[str]]) -> bool:
    if len(rows) < 2:
        return False
    widest = max((len(r) for r in rows), default=0)
    if widest < 2:
        return False
    cells = [c for r in rows for c in r if c]
    if len(cells) < 4:
        return False
    if sum(1 for c in cells if _HEADING_CELL.match(c)) >= 2:  # table of contents: layout, keep as prose
        return False
    numeric = sum(1 for c in cells if _NUMERIC_CELL.match(c) and any(ch.isdigit() for ch in c))
    return numeric / len(cells) >= 0.3


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_tag: str | None = None
        self._skip_depth = 0
        self._parts: list[str] = []
        self._len = 0
        self.tables: list[Table] = []
        self._tdepth = 0
        self._rows: list[list[str]] = []
        self._cell: list[str] | None = None
        self._row: list[str] | None = None

    # -- helpers
    def _emit(self, s: str) -> None:
        self._parts.append(s)
        self._len += len(s)

    def _newline(self) -> None:
        if self._parts and not self._parts[-1].endswith("\n"):
            self._emit("\n")

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [_clean(line) for line in raw.split("\n")]
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()

    # -- parser callbacks
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        style = dict(attrs).get("style") or ""
        if tag in SKIP_TAGS or "display:none" in style.replace(" ", "").lower():
            if tag not in VOID_TAGS:
                self._skip_tag, self._skip_depth = tag, 1
            return
        if tag == "table":
            if self._tdepth == 0:
                self._rows = []
            self._tdepth += 1
        elif self._tdepth and tag == "tr":
            self._row = []
        elif self._tdepth and tag in ("td", "th"):
            self._cell = []
        elif tag in BLOCK_TAGS and not self._tdepth:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag in ("td", "th") and self._tdepth and self._cell is not None:
            if self._row is not None:
                self._row.append(_clean(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._tdepth and self._row is not None:
            if any(self._row):
                self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._tdepth:
            self._tdepth -= 1
            if self._tdepth == 0:
                self._finish_table()
        elif tag in BLOCK_TAGS and not self._tdepth:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self._skip_tag is not None:
            return
        if self._tdepth:
            if self._cell is not None:
                self._cell.append(data)
            return
        self._emit(data)

    def _finish_table(self) -> None:
        rows = [list(r) for r in self._rows]
        if is_data_table(rows):
            # trim empty spacer columns
            width = max(len(r) for r in rows)
            keep = [i for i in range(width) if any(i < len(r) and r[i] for r in rows)]
            rows = [[r[i] if i < len(r) else "" for i in keep] for r in rows]
            self._newline()
            self.tables.append(Table(rows=rows, idx=len(self.tables)))
            self._emit(table_marker(len(self.tables) - 1))
            self._newline()
        else:  # layout table: flatten to prose, one paragraph per row
            for r in rows:
                self._newline()
                self._emit(" ".join(c for c in r if c))
            self._newline()
        self._rows = []


def parse_html(html: str) -> ParsedDocument:
    parser = _Extractor()
    parser.feed(html)
    parser.close()
    return ParsedDocument(text=parser.text(), tables=parser.tables)

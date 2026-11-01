"""HTML -> text/tables -> Items -> chunks -> embedding (pure, no DB, no network)."""

from __future__ import annotations

import itertools
import math

from app.modules.research.chunking import chunk_text, estimate_tokens
from app.modules.research.embedding import EMBEDDING_DIM, HashingEmbedder, cosine, tokenize
from app.modules.research.htmltext import TABLE_MARK, is_data_table, parse_html
from app.modules.research.pipeline import process_8k_html, process_10k_html
from app.modules.research.sections import split_10k
from tests.research.conftest import build_8k_html, build_10k_html


def test_html_drops_hidden_ixbrl_style_and_script() -> None:
    parsed = parse_html(build_10k_html("Acme"))
    assert "HIDDEN_IXBRL_NOISE" not in parsed.text
    assert "color:red" not in parsed.text
    assert "Item 1A. Risk Factors" in parsed.text or "Item 1A." in parsed.text


def test_data_tables_become_json_layout_tables_become_prose() -> None:
    parsed = parse_html(build_10k_html("Acme"))
    assert len(parsed.tables) == 2  # financial table + segment table; the TOC table is layout/prose
    fin = parsed.tables[0].rows
    assert ["Revenue", "$ 1,200", "$ 1,000"] in fin
    assert TABLE_MARK.search(parsed.text)
    assert is_data_table([["a", "1", "2"], ["b", "3", "4"]])
    assert not is_data_table([["Item 1.", "Business"]])
    assert not is_data_table([["only"], ["text"]])


def test_split_picks_real_section_not_table_of_contents() -> None:
    sections = split_10k(parse_html(build_10k_html("Acme")).text)
    assert set(sections) == {"1", "1A", "7", "7A"}
    assert "Platform strategy" in sections["1"].text
    assert "Supply chain concentration" in sections["1A"].text
    assert "Cloud revenue growth" in sections["7"].text
    assert "Unresolved Staff" not in sections["1A"].text
    assert len(sections["1A"].text) > 2000  # not the TOC stub


def test_cross_reference_line_does_not_truncate_item7() -> None:
    sections = split_10k(parse_html(build_10k_html("Acme")).text)
    assert "See Item 1A. Risk Factors for more detail." in sections["7"].text


def test_process_10k_assigns_items_tables_anchors_and_embeddings() -> None:
    doc = process_10k_html(build_10k_html("Acme"))
    assert doc.sections_found == ["1", "1A", "7", "7A"]
    assert {c.item for c in doc.chunks} == {"1", "1A", "7", "7A"}
    assert all(c.embedding is not None and len(c.embedding) == EMBEDDING_DIM for c in doc.chunks)
    assert all(c.anchor.startswith("item-") for c in doc.chunks)
    assert {t.item for t in doc.tables} == {"1", "7"}
    assert doc.quarantined == 0 and doc.thin_sections == []
    assert all(TABLE_MARK.search(c.text) is None for c in doc.chunks)


def test_thin_section_is_reported_not_hidden() -> None:
    doc = process_10k_html(build_10k_html("Acme", thin_item7=True))
    assert "7" in doc.thin_sections


def test_8k_is_single_item_document() -> None:
    doc = process_8k_html(build_8k_html())
    assert doc.sections_found == ["8-K"] and len(doc.chunks) == 1
    assert "press release" in doc.chunks[0].text.lower()


def test_missing_items_are_simply_absent() -> None:
    assert split_10k("Just some text without items.") == {}


def test_chunk_size_overlap_and_coverage() -> None:
    words = [f"W{i}" for i in range(6000)]
    text = ". ".join(" ".join(words[i : i + 12]) for i in range(0, len(words), 12)) + "."
    chunks = chunk_text(text)
    assert len(chunks) >= 6
    sizes = [c.token_count for c in chunks]
    assert max(sizes) <= 800 and sum(sizes[:-1]) / len(sizes[:-1]) > 600
    # overlap: consecutive chunks share trailing/leading content, ~15%
    overlaps = []
    for a, b in itertools.pairwise(chunks):
        wa, wb = a.text.split(), b.text.split()
        shared = len(set(wa[-200:]) & set(wb[:200]))
        overlaps.append(shared / len(wa))
    assert all(0.05 < o < 0.30 for o in overlaps), overlaps
    first_words = {c.text.split()[0] for c in chunks}
    assert len(first_words) == len(chunks)  # progress: every chunk starts somewhere new
    joined = " ".join(c.text for c in chunks)
    assert all(w in joined for w in ("W0", "W3000", "W5999"))  # nothing lost


def test_chunker_handles_degenerate_input() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n \n") == []
    one = chunk_text("A single sentence.")
    assert len(one) == 1 and one[0].chunk_no == 0
    giant = "word " * 5000  # no sentence boundaries at all
    pieces = chunk_text(giant)
    assert len(pieces) > 1 and all(c.token_count <= 800 for c in pieces)
    assert estimate_tokens("one two three") == math.ceil(3 * 1.3)


def test_embedding_is_deterministic_unit_norm_and_lexically_meaningful() -> None:
    e = HashingEmbedder()
    a = e.embed("Revenue growth was driven by cloud services demand")
    assert a == e.embed("Revenue growth was driven by cloud services demand")
    assert abs(sum(x * x for x in a) - 1.0) < 1e-9 and len(a) == 1024
    near = cosine(a, e.embed("cloud services demand drove revenue growth"))
    far = cosine(a, e.embed("litigation reserves for talc claims increased"))
    assert near > far + 0.3
    z = e.embed("")
    assert abs(sum(x * x for x in z) - 1.0) < 1e-9  # defined cosine even for empty text
    assert tokenize("The Company's R&D, and the 10-K") == ["company's", "10-k"] or "company's" in tokenize(
        "The Company's"
    )

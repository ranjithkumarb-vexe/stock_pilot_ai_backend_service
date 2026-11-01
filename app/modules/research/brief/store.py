"""Persistence for briefs and citations (user tables, RLS) and the citation drawer payload."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

from app.core.db import Connection
from app.modules.edgar.client import companyfacts_url
from app.modules.research.brief.generate import BuiltBrief, now_iso, render_markdown
from app.modules.research.brief.schemas import BriefOut, CitationOut

EXCERPT_CHARS = 1200


async def find_cached(
    conn: Connection, user_id: UUID, symbol: str, as_of: date, style_hash: str
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "select id, content, created_at, trace_id, status, stale, stale_reason, as_of from briefs "
        "where user_id = $1 and symbol = $2 and as_of = $3 and style_hash = $4",
        user_id, symbol, as_of, style_hash,
    )  # fmt: skip
    return dict(row) if row else None


async def save_brief(
    conn: Connection,
    user_id: UUID,
    built: BuiltBrief,
    trace_id: str | None,
    created_at_iso: str | None = None,
) -> dict[str, Any]:
    """Insert (replacing any row with the same cache key) the brief and its citations. Returns the stored row."""
    style_hash = built.style.style_hash()
    await conn.execute(
        "delete from briefs where user_id = $1 and symbol = $2 and as_of = $3 and style_hash = $4",
        user_id, built.symbol, built.as_of, style_hash,
    )  # fmt: skip
    brief_id = uuid4()
    row = await conn.fetchrow(
        "insert into briefs (id, user_id, symbol, as_of, style_hash, style_card, ledger, content, rendered_md, "
        "status, stale, stale_reason, trace_id) values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) "
        "returning id, created_at, trace_id, content, status, stale, stale_reason, as_of",
        brief_id, user_id, built.symbol, built.as_of, style_hash, built.style.model_dump(), built.ledger_json,
        built.content, render_markdown(built.content), built.status, built.stale, built.stale_reason, trace_id,
    )  # fmt: skip
    if built.citations:
        await conn.execute(
            "insert into citations (id, brief_id, user_id, kind, ref_id, locator, quote_hash) "
            "select id, $2, $3, k, r, l, q from unnest($1::uuid[], $4::text[], $5::text[], $6::text[], $7::text[]) as t(id, k, r, l, q)",
            [c.id for c in built.citations], brief_id, user_id, [c.kind for c in built.citations],
            [c.ref_id for c in built.citations], [c.locator for c in built.citations], [c.quote_hash for c in built.citations],
        )  # fmt: skip
    assert row is not None
    return dict(row)


def to_brief_out(row: dict[str, Any]) -> BriefOut:
    content: dict[str, Any] = dict(row["content"])
    return BriefOut.model_validate(
        {
            **content,
            "id": str(row["id"]),
            "generated_at": now_iso(row["created_at"]),
            "trace_id": row["trace_id"],
        }
    )


async def citation_detail(conn: Connection, citation_id: UUID) -> CitationOut | None:
    c = await conn.fetchrow(
        "select c.id, c.kind, c.ref_id, c.locator, b.ledger, b.symbol from citations c join briefs b on b.id = c.brief_id "
        "where c.id = $1",
        citation_id,
    )
    if c is None:
        return None
    t = await conn.fetchrow("select name, cik from tickers where symbol = $1", c["symbol"])
    company = f"{t['name']} (CIK {t['cik']})" if t and t["cik"] else (t["name"] if t else c["symbol"])
    if c["kind"] == "chunk":
        ch = await conn.fetchrow(
            "select k.text, k.item, k.form, k.period, k.accession_no, k.filed_at, d.url, d.fetched_at, d.sha256, d.title "
            "from doc_chunks k join raw_documents d on d.id = k.document_id where k.id = $1::uuid",
            c["ref_id"],
        )
        if ch is None:
            return None
        title = {
            "1": "Business",
            "1A": "Risk Factors",
            "7": "Management's Discussion",
            "7A": "Market Risk",
        }.get(ch["item"], ch["item"] or "Filing")
        return CitationOut(
            id=str(c["id"]), kind="chunk", title=title, company=company,
            doc_label=f"{ch['title'] or ch['form']} · Item {ch['item']}", accession_no=ch["accession_no"],
            filed_at=ch["filed_at"].date().isoformat() if ch["filed_at"] else None,
            fetched_at=now_iso(ch["fetched_at"]), sha256=ch["sha256"], url=ch["url"],
            excerpt=ch["text"][:EXCERPT_CHARS], rows=[],
        )  # fmt: skip
    ids = [i for i in str(c["locator"]).removeprefix("ledger:").split(",") if i]
    ledger: dict[str, Any] = c["ledger"]
    entries = [(i, ledger[i]) for i in ids if i in ledger]
    accs = sorted({a for _, e in entries for a in e.get("accession_nos", [])})
    first = entries[0][1] if entries else {}
    excerpt = "; ".join(f"{e['label']}: {e['display']} ({e['source']})" for _, e in entries)
    url = None
    if t and t["cik"] and c["kind"] == "xbrl":
        url = companyfacts_url(t["cik"])
    import hashlib
    import json

    sha = hashlib.sha256(json.dumps([e for _, e in entries], sort_keys=True).encode()).hexdigest()
    return CitationOut(
        id=str(c["id"]), kind="price" if c["kind"] == "price" else "xbrl",
        title="Verified metrics", company=company,
        doc_label="data.sec.gov companyfacts" if c["kind"] == "xbrl" else str(first.get("source", "price")),
        accession_no=accs[0] if accs else None, filed_at=None, fetched_at=None, sha256=sha, url=url,
        excerpt=excerpt[:EXCERPT_CHARS],
        rows=[{"label": e["label"], "value": e["display"], "source": e["source"]} for _, e in entries],
    )  # fmt: skip

"""Static educational definitions and in-app help. No figures here: anything numeric comes from the ledger."""

from __future__ import annotations

GLOSSARY: dict[str, str] = {
    "pe": (
        "The price-to-earnings (P/E) ratio is a company's share price divided by its earnings per share. It "
        "tells you how much the market is paying for each dollar of reported profit; compare it with peers in "
        "the same sector and with the company's own history rather than on its own. StockPilot computes P/E in "
        "code from filed diluted EPS and the latest price."
    ),
    "hhi": (
        "HHI (the Herfindahl-Hirschman Index) measures concentration: add up the squared portfolio weights. A "
        "value near the maximum means almost everything sits in one position; the lower it is, the more evenly spread you "
        "are. The effective number of holdings is the reciprocal of HHI."
    ),
    "10k": (
        "A 10-K is the annual report a US public company files with the SEC. It has the audited financial "
        "statements plus narrative sections such as Business (Item 1), Risk Factors (Item 1A) and Management's "
        "Discussion and Analysis (Item 7). A 10-K is filed once a year and is the main source for StockPilot briefs."
    ),
    "8k": (
        "An 8-K is the current report a company files to announce a material event such as results, an executive "
        "change or an acquisition. Unlike the annual 10-K it is filed as events happen, so it is one of the "
        "sources StockPilot watches for alerts. The difference: the 10-K is the yearly full picture, the 8-K is a "
        "news-style update."
    ),
    "drawdown": (
        "Drawdown is the fall from a portfolio's previous peak to its current value, shown as a percentage. "
        "Maximum drawdown is the largest such fall over a period. It is a measure of how painful the worst "
        "stretch was, and your drawdown rule is the limit you set for it."
    ),
    "beta": (
        "Beta measures how strongly a portfolio or stock has moved with the overall market (SPY here). A beta "
        "above one means it has moved more than the market, below one less. It is backward-looking and says nothing "
        "about the future."
    ),
    "volatility": (
        "Volatility is how much returns swing around their average, usually annualised from daily returns. A "
        "higher figure means a bumpier ride, not a better or worse outcome."
    ),
    "diversification": (
        "Diversification means spreading money across holdings and sectors so that no single position or "
        "industry dominates the result. StockPilot checks it with your position cap, sector cap, HHI and the "
        "effective number of holdings."
    ),
    "fcf": (
        "Free cash flow is the cash a company generates from operations after capital expenditure. StockPilot "
        "computes it from filed XBRL cash-flow facts."
    ),
    "dividend_yield": (
        "Dividend yield is the annual dividend per share divided by the share price. It changes whenever the "
        "price changes and is only meaningful for companies that pay a dividend."
    ),
    "paper": (
        "Paper trading means every order is simulated in StockPilot's own ledger with realistic fills and "
        "slippage. No real order is ever sent to a broker and no real money moves."
    ),
    "xbrl": (
        "XBRL is the machine-readable tagging companies add to their financial statements in SEC filings. "
        "StockPilot reads these tagged facts directly, so reported figures come from the filing rather than "
        "from a model."
    ),
    "eps": (
        "Earnings per share is net income divided by the number of shares; the diluted version also counts "
        "shares that options and convertibles could add. StockPilot reads it from the filing's XBRL facts."
    ),
    "market_cap": (
        "Market capitalisation is the share price multiplied by shares outstanding: the market's current "
        "valuation of the whole company."
    ),
}

HELP: dict[str, str] = {
    "rules": (
        "Open Profile and go to your rules to change a limit such as the position cap. Loosening a rule shows a "
        "reflective warning first, and the change is logged. StockPilot never changes a rule for you."
    ),
    "why": (
        "Use the Why this? control on any feed item, alert or proposal. It lists the top features behind the "
        "ranking and the rules that were checked. It is built from the ranking audit record, not from a language "
        "model."
    ),
    "default": (
        "Most settings live on Profile (rules, style, memories, export and reset) and every suggestion has a "
        "Why this? panel. Ask me about your portfolio, your rules or a company's filings and I will answer from "
        "your data and the filings."
    ),
}


def help_key(message: str) -> str:
    low = message.lower()
    if any(w in low for w in ("rule", "cap", "limit", "position", "sector")):
        return "rules"
    if any(w in low for w in ("why", "suggest", "proposal", "seeing", "ranked", "alert")):
        return "why"
    return "default"

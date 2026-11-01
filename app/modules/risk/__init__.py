"""Risk engine public API (stable; imported by the paper engine, proposals and stream C's ranker).

    from app.modules.risk import compute_metrics, validate, default_rules_for_tier
    from app.modules.risk import Portfolio, Position, Rules, TradeProposal

    result = validate(TradeProposal("AAPL", "buy", Decimal(10), Decimal("250")), portfolio, rules)
    if not result.allowed: ...   # result.violations / result.warnings / result.checks

Pure functions only: no database, clock or network. See `engine.py`.
"""

from app.modules.risk.engine import (
    compute_metrics,
    current_violations,
    curve_summary,
    default_rules_for_tier,
    rule_statuses,
    suggest_reduction,
    validate,
)
from app.modules.risk.models import (
    RULE_KEYS,
    CurveSummary,
    Portfolio,
    PortfolioMetrics,
    Position,
    RuleCheck,
    Rules,
    RuleStatus,
    TradeProposal,
    ValidationResult,
    Violation,
)

__all__ = [
    "RULE_KEYS",
    "CurveSummary",
    "Portfolio",
    "PortfolioMetrics",
    "Position",
    "RuleCheck",
    "RuleStatus",
    "Rules",
    "TradeProposal",
    "ValidationResult",
    "Violation",
    "compute_metrics",
    "current_violations",
    "curve_summary",
    "default_rules_for_tier",
    "rule_statuses",
    "suggest_reduction",
    "validate",
]

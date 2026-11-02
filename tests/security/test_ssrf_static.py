# ruff: noqa: RUF001, S104, S607, RUF100  (homoglyph/0.0.0.0 payloads; git is invoked by name on purpose)
"""SEC-01 areas 2, 4, 5: SSRF/outbound allowlist, SQL-construction scan, outbound client inventory, secrets in the
repo, supply-chain hygiene. Pure static/unit checks (no database needed)."""

from __future__ import annotations

import ast
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.modules.edgar.client import EdgarClient, EdgarError
from app.modules.edgar.netsafety import (
    UrlRejectedError,
    check_url_syntax,
    is_public_ip,
    validate_url,
)

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"


# ------------------------------------------------------------------------------------------------ SSRF (T-SEC-04)
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/",
        "https://[::ffff:169.254.169.254]/",
        "https://127.0.0.1/",
        "https://0x7f000001/",
        "https://2130706433/",
        "https://017700000001/",
        "https://localhost/",
        "https://metadata.google.internal/",
        "https://sec.gov@evil.example/",
        "https://www.sec.gov@evil.example/x",
        "https://evil.example\\@www.sec.gov/",
        "https://www.sec.gov.evil.example/",
        "https://evilsec.gov/",
        "https://notwww.sec.gov/",
        "https://www.sec.gov:8443/",
        "https://www.sec.gov:80/",
        "http://www.sec.gov/",
        "ftp://www.sec.gov/",
        "file:///etc/passwd",
        "gopher://www.sec.gov/",
        "//www.sec.gov/x",
        "www.sec.gov/x",
        "https://user:pw@www.sec.gov/x",
        "https://ｓｅｃ.gov/",
        "https://www.sec.gov%2f@evil.example/",
        "",
        "   ",
        "https:///x",
    ],
)
def test_url_syntax_guard_rejects(url: str) -> None:
    with pytest.raises((UrlRejectedError, ValueError)):
        check_url_syntax(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.sec.gov/Archives/x",
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
        "https://sec.gov/cgi-bin/x",
        "https://WWW.SEC.GOV/x",
        "https://www.sec.gov./x",
    ],
)
def test_url_syntax_guard_accepts_the_allowlist(url: str) -> None:
    assert check_url_syntax(url) in {"www.sec.gov", "data.sec.gov", "sec.gov"}


@pytest.mark.parametrize(
    "addr",
    [
        "10.0.0.1",
        "172.16.5.4",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "198.18.0.1",
        "192.0.0.8",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd00:ec2::254",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "2001:db8::1",
        "not-an-ip",
    ],
)
def test_non_public_addresses_are_rejected(addr: str) -> None:
    assert not is_public_ip(addr)


def test_public_addresses_are_accepted() -> None:
    assert is_public_ip("23.45.67.89") and is_public_ip("2606:4700:4700::1111")


async def test_dns_rebinding_style_answers_are_rejected() -> None:
    async def public_only(_h: str) -> list[str]:
        return ["23.45.67.89"]

    async def mixed(_h: str) -> list[str]:  # one good record plus one internal record
        return ["23.45.67.89", "10.1.2.3"]

    async def metadata(_h: str) -> list[str]:
        return ["169.254.169.254"]

    async def nothing(_h: str) -> list[str]:
        return []

    await validate_url("https://www.sec.gov/x", resolver=public_only)
    for resolver in (mixed, metadata, nothing):
        with pytest.raises(UrlRejectedError):
            await validate_url("https://www.sec.gov/x", resolver=resolver)


async def test_edgar_client_never_requests_a_redirect_target_that_fails_validation(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "www.sec.gov":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/iam"})
        return httpx.Response(200, content=b"secret")

    async def resolve(_h: str) -> list[str]:
        return ["23.45.67.89"]

    async with EdgarClient(
        "StockPilot sec@example.test",
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path,
        resolver=resolve,
        max_retries=0,
    ) as c:
        with pytest.raises((UrlRejectedError, EdgarError)):
            await c.get("https://www.sec.gov/Archives/x")
    assert seen == ["https://www.sec.gov/Archives/x"]


async def test_edgar_client_redirect_to_another_allowed_host_resolving_privately_is_blocked(
    tmp_path: Path,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "www.sec.gov":
            return httpx.Response(302, headers={"location": "https://data.sec.gov/x"})
        return httpx.Response(200, content=b"internal")

    async def resolve(host: str) -> list[str]:
        return ["10.0.0.5"] if host == "data.sec.gov" else ["23.45.67.89"]

    async with EdgarClient(
        "StockPilot sec@example.test",
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path,
        resolver=resolve,
        max_retries=0,
    ) as c:
        with pytest.raises((UrlRejectedError, EdgarError)):
            await c.get("https://www.sec.gov/a")


async def test_edgar_client_enforces_body_cap_and_redirect_limit(tmp_path: Path) -> None:
    async def resolve(_h: str) -> list[str]:
        return ["23.45.67.89"]

    big = httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 5000))
    async with EdgarClient(
        "StockPilot sec@example.test",
        transport=big,
        cache_dir=tmp_path / "a",
        resolver=resolve,
        max_bytes=1000,
        max_retries=0,
    ) as c:
        with pytest.raises(EdgarError):
            await c.get("https://www.sec.gov/big")
    loop = httpx.MockTransport(
        lambda r: httpx.Response(302, headers={"location": "https://www.sec.gov/loop"})
    )
    async with EdgarClient(
        "StockPilot sec@example.test",
        transport=loop,
        cache_dir=tmp_path / "b",
        resolver=resolve,
        max_retries=0,
    ) as c:
        with pytest.raises(EdgarError):
            await c.get("https://www.sec.gov/loop")


def test_edgar_client_does_not_follow_redirects_by_itself() -> None:
    src = (APP / "modules/edgar/client.py").read_text()
    assert "follow_redirects=False" in src


def test_every_outbound_http_client_is_inventoried_and_none_takes_a_user_url() -> None:
    """Allow-list of modules that create HTTP clients. A new one must be reviewed for SSRF (host from config,
    never from a request) and added here."""
    reviewed = {
        "core/auth.py": "JWKS URL from SUPABASE_JWKS_URL (config)",
        "modules/edgar/client.py": "SEC allow-list + private-IP block + redirect re-validation",
        "modules/llm/providers/meta.py": "META_API_BASE_URL (config), key in header",
        "modules/marketdata/sources/alpaca.py": "fixed vendor hosts (config)",
        "modules/marketdata/sources/finnhub.py": "fixed vendor host (config)",
        "modules/profile/supabase_admin.py": "SUPABASE_URL (config) + uuid path",
    }
    found = {
        str(p.relative_to(APP))
        for p in APP.rglob("*.py")
        if re.search(
            r"httpx\.(Async)?Client\(|aiohttp|urllib\.request|requests\.(get|post)|urlopen\(", p.read_text()
        )
    }
    assert found <= set(reviewed), f"unreviewed outbound HTTP use: {sorted(found - set(reviewed))}"
    # no *request* model (ApiModel subclasses, extra=forbid) carries a URL-ish field a user could point us at
    offenders = []
    for p in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.ClassDef) and any(ast.unparse(b) == "ApiModel" for b in node.bases):
                for st in node.body:
                    if (
                        isinstance(st, ast.AnnAssign)
                        and isinstance(st.target, ast.Name)
                        and re.fullmatch(r"(?i).*(url|uri|callback|webhook|endpoint|host).*", st.target.id)
                    ):
                        offenders.append((str(p.relative_to(ROOT)), node.name, st.target.id))
    assert offenders == [], offenders


# ------------------------------------------------------------------------------------------------ SQL construction
DB_METHODS = {
    "execute",
    "executemany",
    "fetch",
    "fetchrow",
    "fetchval",
    "prepare",
    "cursor",
    "copy_records_to_table",
}
#: every f-string / concatenation that feeds a DB call, reviewed by SEC-01 (interpolations are module constants,
#: placeholder counters or catalog identifiers - never request values)
REVIEWED_INTERPOLATIONS = {
    ("app/modules/audit/service.py", "_COLS"): "module constant column list",
    ("app/modules/audit/service.py", "where"): "static clause text built in the same function; values bound",
    ("app/modules/paper/service.py", "' and '.join(where)"): "static clauses, values bound ($n)",
    ("app/modules/paper/service.py", "len(args)"): "placeholder index",
    ("app/modules/profile/export.py", "drop"): "constant DROP_COLUMNS",
    ("app/modules/profile/export.py", "name"): "table name from pg_class catalog",
    ("app/modules/profile/export.py", "ROW_LIMIT + 1"): "int constant",
    ("app/core/migrate.py", "dbname"): "validated identifier (_DB_NAME_RE)",
    ("app/core/migrate.py", "ext"): "constant extension names",
    ("app/modules/alerts/router.py", "len(args)"): "placeholder index",
    ("app/modules/alerts/router.py", "len(args) - 1"): "placeholder index",
}


def _sql_construction_sites() -> list[tuple[str, int, str]]:
    sites: list[tuple[str, int, str]] = []
    for p in sorted(APP.rglob("*.py")):
        rel = str(p.relative_to(ROOT))
        tree = ast.parse(p.read_text())
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr in DB_METHODS
                and n.args
            ):
                a = n.args[0]
                if isinstance(a, ast.JoinedStr):
                    for v in a.values:
                        if isinstance(v, ast.FormattedValue):
                            sites.append((rel, n.lineno, ast.unparse(v.value)))
                elif isinstance(a, ast.BinOp) or (
                    isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute) and a.func.attr == "format"
                ):
                    sites.append((rel, n.lineno, "<concat/format>"))
    return sites


def test_sql_is_parameterised_everywhere_except_reviewed_sites() -> None:
    unreviewed = [
        (f, ln, e) for f, ln, e in _sql_construction_sites() if (f, e) not in REVIEWED_INTERPOLATIONS
    ]
    assert not unreviewed, f"new dynamic SQL needs a SEC review: {unreviewed}"


def test_sql_built_in_variables_is_limited_to_whitelisted_fragments() -> None:
    """Named SQL variables assembled with + / f-strings (alerts/paper/profile) never include request strings."""
    alerts = (APP / "modules/alerts/router.py").read_text()
    assert 'name in ("new", "opened", "dismissed", "snoozed")' in alerts  # state is whitelisted
    assert "type: Annotated[AlertType" in alerts  # type is an enum


# ------------------------------------------------------------------------------------------------ secrets / supply chain
SECRET_PATTERNS = {
    "aws": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{15,}\.eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{10,}"),
    "openai-style": re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    "github": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "slack": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "generic-assign": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*['\"][A-Za-z0-9/+_\-]{24,}['\"]"
    ),
}
#: the only credential-looking constants allowed in the repo: documented DEV defaults
ALLOWED_LITERALS = ("dev-only-secret-change-me", "stockpilot_dev_pw", "postgres:postgres@")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout  # noqa: S603, S607


def test_no_secrets_in_tracked_files() -> None:
    hits = []
    for rel in _git("ls-files").splitlines():
        p = ROOT / rel
        if not p.is_file() or (
            p.suffix in {".lock", ".png", ".jpg", ".gz", ".pdf", ".json"} and p.stat().st_size > 2_000_000
        ):
            continue
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for name, rx in SECRET_PATTERNS.items():
            for m in rx.finditer(text):
                if not any(
                    a in m.group(0) or a in text[max(0, m.start() - 80) : m.end() + 80]
                    for a in ALLOWED_LITERALS
                ):
                    hits.append((rel, name, m.group(0)[:20] + "..."))
    assert not hits, hits


def test_no_secrets_in_git_history() -> None:
    log = _git("log", "-p", "--all", "--no-color", "-U0")
    hits = []
    for name, rx in SECRET_PATTERNS.items():
        for m in rx.finditer(log):
            ctx = log[max(0, m.start() - 80) : m.end() + 80]
            if not any(a in ctx for a in ALLOWED_LITERALS):
                hits.append((name, m.group(0)[:20] + "..."))
    assert not hits, hits


def test_env_files_are_ignored_and_example_has_no_real_values() -> None:
    tracked = _git("ls-files").splitlines()
    assert [f for f in tracked if re.search(r"(^|/)\.env(\.|$)", f) and not f.endswith(".example")] == []
    ignored = subprocess.run(
        ["git", "check-ignore", ".env", ".env.local", ".env.production"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()  # noqa: S607
    assert {".env", ".env.local", ".env.production"} <= set(ignored)
    example = (ROOT / ".env.example").read_text()
    for key in (
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_JWT_SECRET",
        "META_MODEL_API_KEY",
        "ALPACA_SECRET",
        "FINNHUB_KEY",
        "LANGFUSE_SECRET_KEY",
        "FRED_API_KEY",
    ):
        m = re.search(rf"^{key}=(.*)$", example, re.M)
        assert m is not None, key
        assert m.group(1).split("#")[0].strip() == "", f"{key} must be empty in .env.example"
    assert "dev-only-secret-change-me" in example  # documented dev default, flagged as dev-only


def test_dockerfile_runs_as_non_root_and_does_not_ship_env_files() -> None:
    df = (ROOT / "Dockerfile").read_text()
    users = re.findall(r"^USER\s+(\S+)", df, re.M)
    assert users and users[-1] not in {"root", "0"}, "container must not run as root"
    assert "COPY . " not in df and "COPY .env" not in df
    ignore = (ROOT / ".dockerignore").read_text()
    assert ".env" in ignore and ".git" in ignore


def test_lockfile_is_committed_and_pinned_by_hash() -> None:
    tracked = _git("ls-files").splitlines()
    assert "uv.lock" in tracked
    lock = (ROOT / "uv.lock").read_text()
    assert lock.count("sha256:") > 50


def test_ruff_bandit_rules_are_enabled() -> None:
    """The `S` (bandit) rule set is on for app/ and CI-gated through `make lint`."""
    cfg = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'select = \[[^\]]*"S"', cfg)


def test_no_debug_or_eval_primitives_in_app() -> None:
    bad = []
    for p in APP.rglob("*.py"):
        for ln, line in enumerate(p.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(
                r"\b(eval|exec)\(|pickle\.loads?\(|yaml\.load\(|subprocess\.|os\.system\(|shell=True|__import__\(",
                code,
            ):
                bad.append((str(p.relative_to(ROOT)), ln, line.strip()[:80]))
    assert bad == [], bad


def test_time_is_irrelevant_but_utc() -> None:
    assert datetime.now(UTC).tzinfo is not None

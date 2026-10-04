"""Provider-independent public research. No account data, arbitrary queries, or redirects."""

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

SERVICES = {
    "brave": frozenset({"web-search"}),
    "dart": frozenset({"disclosures"}),
    "krx": frozenset({"kospi-daily", "kosdaq-daily"}),
}
TOPIC_LABELS = {
    "earnings": "실적 공시",
    "disclosure": "주요사항 공시",
    "valuation": "기업 가치 재무",
    "business": "사업 IR",
    "macro": "산업 경제 공식 통계",
    "litigation": "소송 규제 공시",
}


def get_json(host, path, headers=None):
    connection = http.client.HTTPSConnection(host, timeout=12)
    try:
        connection.request("GET", path, headers={"Accept": "application/json", **(headers or {})})
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 1024 * 1024:
            raise ValueError("research_provider_unavailable")
        return json.loads(raw)
    finally:
        connection.close()


def public_url(url):
    if not isinstance(url, str) or len(url) > 2000:
        raise ValueError("invalid_source_url")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or "\\" in url
        or any(ord(c) < 33 for c in url)
    ):
        raise ValueError("unsafe_source_url")
    host = parsed.hostname.encode("idna").decode()
    addresses = {r[4][0] for r in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError("private_source_address")
    return parsed, host, sorted(addresses)[0]


class PublicConnection(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=8)
        self.address = address

    def connect(self):
        # Pin the validated IP while checking TLS against the original host: no DNS rebinding.
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, value):
        if not self.hidden and value.strip():
            self.parts.append(value.strip())


def fetch_excerpt(url):
    parsed, host, address = public_url(url)
    connection = PublicConnection(host, address)
    try:
        path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
        connection.request(
            "GET",
            path,
            headers={"User-Agent": "VeyquantResearch/0.12", "Accept": "text/html,text/plain"},
        )
        response = connection.getresponse()
        # No redirect, decompression bomb, private PDF parser, or cookie-bearing session.
        if response.status != 200 or response.getheader("Content-Encoding"):
            raise ValueError("source_fetch_failed")
        if response.getheader("Content-Type", "").split(";")[0] not in {"text/html", "text/plain"}:
            raise ValueError("unsupported_source_content")
        raw = response.read(131073)
        if len(raw) > 131072:
            raw = raw[:131072]
        parser = PageText()
        parser.feed(raw.decode("utf-8", errors="replace"))
        return " ".join(parser.parts)[:6000]
    finally:
        connection.close()


def public_query(symbol, name, topic):
    if (
        not re.fullmatch(r"[0-9A-Z]{6}", symbol)
        or not isinstance(name, str)
        or not 1 <= len(name) <= 100
        or topic not in TOPIC_LABELS
    ):
        raise ValueError("invalid_public_query")
    # Caller cannot supply free text from the account, model or manual instruction as a query.
    return f"{name} {symbol} {TOPIC_LABELS[topic]}"


def gateway_request(method, params=None):
    """Stateless MCP, signed with the Lambda role. Never accept a caller-supplied endpoint."""
    url = os.environ.get("AGENTCORE_SEARCH_URL", "")
    parsed = urlsplit(url)
    match = re.fullmatch(
        r"[a-z0-9-]+\.gateway\.bedrock-agentcore\."
        r"(ap-northeast-1|us-east-1|eu-west-1)\.amazonaws\.com",
        parsed.hostname or "",
    )
    if (
        not match
        or parsed.scheme != "https"
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise ValueError("web_search_not_configured")
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "veyquant-search",
            "method": method,
            **({"params": params} if params is not None else {}),
        }
    ).encode()
    credentials = boto3.Session(region_name=match[1]).get_credentials()
    if credentials is None:
        raise ValueError("web_search_iam_unavailable")
    request = AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    SigV4Auth(credentials.get_frozen_credentials(), "bedrock-agentcore", match[1]).add_auth(request)
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=25)
    try:
        connection.request("POST", "/mcp", body=body, headers=dict(request.headers))
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"web_search_http_{response.status}")
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("web_search_response_too_large")
        if "text/event-stream" in response.getheader("Content-Type", ""):
            messages = [
                json.loads(line[5:].strip())
                for line in raw.decode().splitlines()
                if line.startswith("data:")
            ]
            value = next((m for m in messages if m.get("id") == "veyquant-search"), {})
        else:
            value = json.loads(raw)
        if value.get("error") or not isinstance(value.get("result"), dict):
            raise ValueError("web_search_gateway_failed")
        return value["result"]
    finally:
        connection.close()


def search_status():
    result = gateway_request("tools/list")
    names = [t.get("name") for t in result.get("tools", [])]
    if "web-search___WebSearch" not in names:
        raise ValueError("web_search_tool_unavailable")
    return {"connected": True, "provider": "Amazon Bedrock AgentCore", "api_key_required": False}


def agentcore_search(symbol, name, topic, now):
    query = public_query(symbol, name, topic)
    result = gateway_request(
        "tools/call",
        {"name": "web-search___WebSearch", "arguments": {"query": query, "maxResults": 5}},
    )
    if result.get("isError"):
        raise ValueError("web_search_failed")
    structured = result.get("structuredContent")
    if not isinstance(structured, dict) or "results" not in structured:
        content = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
        if len(content) != 1:
            raise ValueError("invalid_search_results")
        structured = json.loads(content[0])
    rows = structured.get("results")
    if not isinstance(rows, list):
        raise ValueError("invalid_search_results")
    normalized = [
        {
            "url": r.get("url"),
            "title": r.get("title"),
            "description": r.get("text"),
            "page_age": r.get("publishedDate"),
        }
        for r in rows[:5]
        if isinstance(r, dict)
    ]
    return source_records(normalized, symbol, query, now, "Amazon Bedrock AgentCore")


def search(key, symbol, name, topic, now):
    """Legacy provider compatibility; new installations use agentcore_search."""
    query = public_query(symbol, name, topic)
    data = get_json(
        "api.search.brave.com",
        "/res/v1/web/search?"
        + urlencode(
            {
                "q": query,
                "country": "KR",
                "search_lang": "ko",
                "count": 5,
                "extra_snippets": "true",
                "safesearch": "moderate",
            }
        ),
        {"X-Subscription-Token": key},
    )
    rows = data.get("web", {}).get("results", [])
    if not isinstance(rows, list):
        raise ValueError("invalid_search_results")
    return source_records(rows, symbol, query, now, "Brave Search")


def source_records(rows, symbol, query, now, provider):
    sources = []
    for row in rows[:5]:
        url = row.get("url")
        try:
            # Validate before displaying or fetching a provider-supplied URL.
            public_url(url)
        except (ValueError, OSError):
            continue
        source = {
            "source_group": hashlib.sha256(url.encode()).hexdigest(),
            "kind": "web",
            "symbol": symbol,
            "source": provider,
            "url": url,
            "title": str(row.get("title", ""))[:300],
            "collected_at": now,
            "as_of": None,
            "published_at": row.get("page_age"),
            "excerpt": str(row.get("description", ""))[:2500],
            "status": "search_snippet",
            "query": query,
            "trust": "untrusted_external_evidence",
        }
        try:
            source["page_excerpt"] = fetch_excerpt(url)
            source["status"] = "page_fetched"
        except (ValueError, OSError, http.client.HTTPException):
            source["fetch_status"] = "unavailable"
        source["id"] = hashlib.sha256(
            json.dumps(
                [url, source["excerpt"], source.get("page_excerpt")],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        sources.append(source)
    return sources


DART_USER_AGENT = "Veyquant/0.15.1 (+https://github.com/xtower-studio/veyquant-ai)"
DART_STATUS_ERRORS = {
    "010": "dart_key_unregistered",
    "011": "dart_key_disabled",
    "012": "dart_ip_denied",
    "020": "dart_rate_limited",
    "021": "dart_invalid_request",
    "100": "dart_invalid_request",
    "101": "dart_access_denied",
    "800": "dart_maintenance",
    "900": "dart_unavailable",
    "901": "dart_account_expired",
}
DART_CONNECTION_ERRORS = frozenset(DART_STATUS_ERRORS.values()) | {
    "dart_timeout",
    "dart_connection_unavailable",
    "dart_invalid_response",
}


def connection_error(provider, error):
    # Only our fixed codes cross the Lambda/bridge/API boundary. Never reflect
    # provider messages, exception details or the key-bearing request URL.
    code = str(error)
    return (
        code
        if provider == "dart" and code in DART_CONNECTION_ERRORS
        else "provider_connection_failed"
    )


def dart_list(key, now, page=1, page_count=100):
    if type(page) is not int or not 1 <= page <= 100:
        raise ValueError("invalid_disclosure_page")
    if type(page_count) is not int or not 1 <= page_count <= 100:
        raise ValueError("invalid_disclosure_page_count")
    day = datetime.fromtimestamp(now, ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    path = "/api/list.json?" + urlencode(
        {
            "crtfc_key": key,
            "bgn_de": day,
            "end_de": day,
            "page_no": page,
            "page_count": page_count,
            "sort": "date",
            "sort_mth": "desc",
            "last_reprt_at": "N",
        }
    )
    try:
        data = get_json(
            "opendart.fss.or.kr",
            path,
            {"User-Agent": DART_USER_AGENT},
        )
    except TimeoutError:
        raise ValueError("dart_timeout") from None
    except (ValueError, OSError, http.client.HTTPException):
        raise ValueError("dart_connection_unavailable") from None
    if not isinstance(data, dict):
        raise ValueError("dart_invalid_response")
    if data.get("status") not in {"000", "013"}:
        raise ValueError(DART_STATUS_ERRORS.get(data.get("status"), "dart_unavailable"))
    return data


def disclosures(key, now, page=1):
    data = dart_list(key, now, page)
    if data["status"] == "013":
        return {"events": [], "pages": 0}
    events = []
    for row in data.get("list", []):
        symbol, receipt = row.get("stock_code", ""), row.get("rcept_no", "")
        if (
            not isinstance(symbol, str)
            or not isinstance(receipt, str)
            or not re.fullmatch(r"[0-9A-Z]{6}", symbol)
            or not re.fullmatch(r"\d{14}", receipt)
        ):
            continue
        # Conservative documented classification, not a claim of semantic completeness.
        title = row.get("report_nm", "")
        important = any(
            term in title
            for term in (
                "주요사항",
                "실적",
                "감사",
                "부도",
                "회생",
                "파산",
                "횡령",
                "배임",
                "유상증자",
                "무상증자",
                "감자",
                "합병",
                "분할",
                "공개매수",
                "영업",
                "소송",
                "거래정지",
                "상장폐지",
            )
        )
        events.append(
            {
                "id": "dart:" + receipt,
                "kind": "dart_important" if important else "dart",
                "symbol": symbol,
                "title": title[:300],
                "source": "DART",
                "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=" + receipt,
                "as_of": row.get("rcept_dt"),
                "collected_at": now,
                "status": "filing_index",
                "importance_rule": "material-title-v1",
            }
        )
    return {"events": events, "pages": int(data["total_page"])}


def verify_service(provider, key, now):
    if provider == "brave":
        get_json(
            "api.search.brave.com",
            "/res/v1/web/search?"
            + urlencode(
                {
                    "q": "금융감독원",
                    "count": 1,
                    "country": "KR",
                    "search_lang": "ko",
                }
            ),
            {"X-Subscription-Token": key},
        )
    elif provider == "dart":
        # Authentication is independent of parsing today's filings, including
        # non-listed companies. A valid key with no disclosures is connected.
        dart_list(key, now, page_count=1)
    elif provider == "krx":
        krx_daily(key, now)
    else:
        raise ValueError("invalid_research_provider")
    return list(SERVICES[provider])


def krx_daily(key, now):
    """Two whole-market daily endpoints; published from 08:00 on the following day.

    Search back across holidays, never treat an auth/schema error as an empty market.
    Both services must be authorized. Daily data never replaces broker execution prices.
    """
    today = datetime.fromtimestamp(now, ZoneInfo("Asia/Seoul"))
    first = 1 if today.hour >= 8 else 2
    for offset in range(first, first + 12):
        day = (today - timedelta(days=offset)).strftime("%Y%m%d")
        combined = []
        for market, endpoint in (("KOSPI", "stk_bydd_trd"), ("KOSDAQ", "ksq_bydd_trd")):
            data = get_json(
                "data-dbg.krx.co.kr",
                f"/svc/apis/sto/{endpoint}?" + urlencode({"basDd": day}),
                {"AUTH_KEY": key},
            )
            rows = data.get("OutBlock_1")
            if not isinstance(rows, list) or len(rows) > 5000:
                raise ValueError("invalid_krx_response")
            if not rows:
                break
            for row in rows:
                symbol = row.get("ISU_CD", "")
                if not re.fullmatch(r"[0-9A-Z]{6}", symbol):
                    raise ValueError("invalid_krx_symbol")
                if str(row.get("BAS_DD", "")).replace("/", "").replace("-", "") != day:
                    raise ValueError("stale_krx_response")
                if row.get("MKT_NM") != market:
                    raise ValueError("invalid_krx_market")
                numbers = {}
                for source, field in (
                    ("TDD_CLSPRC", "close"),
                    ("ACC_TRDVOL", "volume"),
                    ("ACC_TRDVAL", "turnover_krw"),
                    ("MKTCAP", "market_cap_krw"),
                    ("LIST_SHRS", "listed_shares"),
                ):
                    raw = str(row.get(source, "")).replace(",", "")
                    if raw in {"", "-"}:
                        numbers[field] = None
                        continue
                    try:
                        value = Decimal(raw)
                        if not value.is_finite() or not 0 <= value <= 10**20:
                            raise ValueError("invalid_krx_number")
                    except InvalidOperation:
                        raise ValueError("invalid_krx_number") from None
                    numbers[field] = str(value)
                combined.append({"symbol": symbol, "market": market, **numbers})
        else:
            return {"as_of": day, "collected_at": now, "rows": combined, "source": "KRX"}
    raise ValueError("krx_daily_not_published")

import copy
import json
import socket

import pytest

from veyquant import research_sources as sources
from veyquant.decision_pipeline import DecisionPipeline
from veyquant.decision_runtime import Runtime
from veyquant.decision_store import DecisionStore


def test_agentcore_search_requires_no_key_and_preserves_citations(monkeypatch):
    calls = []

    def request(method, params):
        calls.append((method, params))
        return {
            "isError": False,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "results": [
                                {
                                    "url": "https://example.org/ir",
                                    "title": "실적 발표",
                                    "text": "발표 근거",
                                    "publishedDate": "2026-09-09",
                                }
                            ]
                        }
                    ),
                }
            ],
        }

    monkeypatch.setattr(sources, "gateway_request", request)
    monkeypatch.setattr(sources, "public_url", lambda u: (None, "example.org", "8.8.8.8"))
    monkeypatch.setattr(sources, "fetch_excerpt", lambda u: "확인된 원문")
    found = sources.agentcore_search("005930", "삼성전자", "earnings", 1000)
    assert calls == [
        (
            "tools/call",
            {
                "name": "web-search___WebSearch",
                "arguments": {"query": "삼성전자 005930 실적 공시", "maxResults": 5},
            },
        )
    ]
    assert found[0]["source"] == "Amazon Bedrock AgentCore"
    assert found[0]["published_at"] == "2026-09-09"
    assert found[0]["url"] == "https://example.org/ir"
    assert found[0]["status"] == "page_fetched"


def test_agentcore_error_cannot_be_treated_as_no_news(monkeypatch):
    monkeypatch.setattr(sources, "gateway_request", lambda *a: {"isError": True})
    with pytest.raises(ValueError, match="web_search_failed"):
        sources.agentcore_search("005930", "삼성전자", "earnings", 1000)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/mcp",
        "https://169.254.169.254/mcp",
        "https://test.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com/mcp?secret=1",
    ],
)
def test_agentcore_never_sends_iam_signature_to_arbitrary_endpoints(monkeypatch, url):
    monkeypatch.setenv("AGENTCORE_SEARCH_URL", url)
    monkeypatch.setattr(sources.boto3, "Session", lambda **kw: pytest.fail("no credentials access"))
    with pytest.raises(ValueError, match="web_search_not_configured"):
        sources.gateway_request("tools/list")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com:8443",
        "https://example.com\\@localhost",
        "file:///etc/passwd",
    ],
)
def test_unsafe_source_urls_rejected_before_dns(url, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("must not resolve unsafe URL")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    with pytest.raises(ValueError):
        sources.public_url(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "fd00::1"])
def test_private_and_mixed_dns_answers_are_rejected(address, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", (address, 443))],
    )
    with pytest.raises(ValueError, match="private_source_address"):
        sources.public_url("https://news.example.org/article")


def test_validated_dns_address_is_pinned_without_second_lookup(monkeypatch):
    connected = []

    class TLS:
        def wrap_socket(self, raw, server_hostname):
            assert server_hostname == "news.example.org"
            return raw

    monkeypatch.setattr(
        socket, "create_connection", lambda address, timeout: connected.append(address) or object()
    )
    monkeypatch.setattr(sources.ssl, "create_default_context", TLS)
    sources.PublicConnection("news.example.org", "8.8.8.8").connect()
    assert connected == [("8.8.8.8", 443)]


def test_search_retains_source_and_distinguishes_missing_page(monkeypatch):
    monkeypatch.setattr(sources, "public_url", lambda u: (None, "example.org", "8.8.8.8"))
    calls = []

    def get(host, path, headers):
        calls.append((host, path))
        return {
            "web": {
                "results": [
                    {"url": "https://example.org/a", "title": "보고서", "description": "내용"}
                ]
            }
        }

    monkeypatch.setattr(sources, "get_json", get)
    monkeypatch.setattr(
        sources, "fetch_excerpt", lambda u: (_ for _ in ()).throw(ValueError("missing"))
    )
    result = sources.search("not-a-real-key", "005930", "삼성전자", "earnings", 1000)
    assert result[0]["status"] == "search_snippet"
    assert result[0]["fetch_status"] == "unavailable"
    assert result[0]["collected_at"] == 1000 and result[0]["as_of"] is None
    assert "not-a-real-key" not in str(calls)
    with pytest.raises(ValueError):
        sources.search("key", "005930", "삼성전자", "cash account secret", 1000)


def test_dart_absence_differs_from_failure_and_material_filing_is_classified(monkeypatch):
    response = {"status": "013"}
    monkeypatch.setattr(sources, "get_json", lambda *a: response)
    assert sources.disclosures("fixture", 1000)["events"] == []
    response = {"status": "020"}
    with pytest.raises(ValueError):
        sources.disclosures("fixture", 1000)
    response = {
        "status": "000",
        "total_page": 1,
        "list": [
            {
                "stock_code": "005930",
                "rcept_no": "20260909000001",
                "report_nm": "주요사항보고서(유상증자결정)",
                "rcept_dt": "20260909",
            }
        ],
    }
    result = sources.disclosures("fixture", 1000)
    assert result["events"][0]["kind"] == "dart_important"
    assert result["events"][0]["url"].endswith("20260909000001")


@pytest.mark.parametrize(
    "level,trigger,launches", [("NORMAL", True, 0), ("WARN", True, 0), ("CRITICAL", False, 1)]
)
async def test_surveillance_routes_only_on_actual_severity(tmp_path, level, trigger, launches):
    runtime = Runtime.__new__(Runtime)
    runtime.clock = lambda: 1000
    runtime.store = DecisionStore(tmp_path / "decisions")
    runtime.configuration = lambda: {"version": 1}

    async def capability(*args, **kwargs):
        return {
            "result": {"severity": level, "trigger_decision": trigger, "summary": "판단"},
            "trace": [],
        }

    requests = []

    async def launch(*args):
        requests.append(args)

    runtime.capability, runtime.launch = capability, launch
    await runtime.surveillance([{"symbol": "005930"}], {"version": 1})
    assert len(requests) == launches
    assert runtime.store.recent(1000)[0]["severity"] == level
    runtime.store.db.close()


async def test_news_count_and_frozen_market_tools(tmp_path):
    async def model(*args):
        return {"summary": "확인된 뉴스", "evidence_ids": ["source"], "uncertainties": []}, {}

    calls = []

    async def news(*args):
        calls.append(args)
        return [{"id": "source", "source": "fixture"}]

    store = DecisionStore(tmp_path / "decisions")
    pipeline = DecisionPipeline(model, None, news, store)
    frozen = {"details": {"005930": {"quote": {"price": "100"}}}}
    original = copy.deepcopy(frozen)
    request = {
        "action": "READ",
        "tool": "news",
        "arguments": {"symbols": ["005930"], "topics": ["earnings"]},
    }
    for _ in range(2):
        await pipeline.read_tool(request, frozen, {}, pipeline.monotonic() + 10)
    await pipeline.read_tool(request, frozen, {}, pipeline.monotonic() + 10)
    assert len(calls) == 1 and frozen == original
    assert pipeline.search_cache_hits == 2
    store.db.close()


def test_dart_verification_uses_identifiable_agent_and_one_result_without_parsing_filings(
    monkeypatch,
):
    from urllib.parse import parse_qs, urlsplit

    requests = []

    class Response:
        status = 200

        def read(self, maximum):
            return b'{"status":"000","list":[{"stock_code":null}]}'

    class Connection:
        def __init__(self, host, timeout):
            assert host == "opendart.fss.or.kr"

        def request(self, method, path, headers):
            assert headers["User-Agent"].startswith("Veyquant/")
            assert headers["Accept"] == "application/json"
            requests.append(parse_qs(urlsplit(path).query))

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(sources.http.client, "HTTPSConnection", Connection)
    assert sources.verify_service("dart", "fixture", 1000) == ["disclosures"]
    assert requests[0]["page_count"] == ["1"]
    assert requests[0]["bgn_de"] == requests[0]["end_de"]


@pytest.mark.parametrize("status,code", list(sources.DART_STATUS_ERRORS.items()))
def test_dart_errors_keep_only_safe_codes(monkeypatch, status, code):
    monkeypatch.setattr(
        sources, "get_json", lambda *a: {"status": status, "message": "private-key"}
    )
    with pytest.raises(ValueError, match=code) as error:
        sources.verify_service("dart", "private-key", 1000)
    assert "private-key" not in str(error.value)


def test_dart_no_disclosures_still_verifies_and_transport_does_not_blame_key(monkeypatch):
    monkeypatch.setattr(sources, "get_json", lambda *a: {"status": "013"})
    assert sources.verify_service("dart", "fixture", 1000) == ["disclosures"]

    def fail(*args):
        raise OSError("private key URL reflected by transport")

    monkeypatch.setattr(sources, "get_json", fail)
    with pytest.raises(ValueError, match="^dart_connection_unavailable$"):
        sources.verify_service("dart", "fixture", 1000)

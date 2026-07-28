# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx>=0.27",
#     "rich>=13",
#     "typer>=0.12",
#     "pytest>=8",
# ]
# ///

"""Offline tests for sdf.py (httpx.MockTransport + typer.testing.CliRunner)."""

import json

import httpx
import pytest
from typer.testing import CliRunner

import sdf

runner = CliRunner()

GOOD_PAYLOAD = {
    "domain": "example.com",
    "count": 3,
    "total": 3,
    "subdomains": ["www.example.com", "mail.example.com", "api.example.com"],
}
EMPTY_PAYLOAD = {"domain": "example.com", "count": 0, "total": 0, "subdomains": []}
TRUNCATED_PAYLOAD = {
    "domain": "example.com",
    "count": 2,
    "total": 5000,
    "subdomains": ["www.example.com", "mail.example.com"],
}


@pytest.fixture
def api(monkeypatch):
    """Route sdf.create_client through a MockTransport serving queued responses."""
    state = {"responses": [], "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if not state["responses"]:
            raise AssertionError("unexpected extra HTTP request")
        item = state["responses"].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def factory(timeout: float) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(sdf, "create_client", factory)
    return state


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sdf.time, "sleep", lambda *_args: None)


def combined(result) -> str:
    try:
        return (result.output or "") + (result.stderr or "")
    except Exception:  # older click mixes stderr into output
        return result.output or ""


def json_response(payload: dict, status: int = 200, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


# --- normalize_domain ------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("example.com", "example.com"),
        ("EXAMPLE.com", "example.com"),
        ("  example.com  ", "example.com"),
        ("www.example.com", "example.com"),
        ("*.example.com", "example.com"),
        ("example.com/", "example.com"),
        ("https://www.example.com/about", "example.com"),
        ("http://example.com:8080/path?q=1#frag", "example.com"),
        ("user:pass@example.com", "example.com"),
        ("blog.example.com", "blog.example.com"),
        ("example.co.uk", "example.co.uk"),
    ],
)
def test_normalize_domain_ok(raw, expected):
    assert sdf.normalize_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "example", "exa mple.com", "-bad.com", "bad-.com", "example.c", "a..com", "http://", "exam_ple.com", "123.45"],
)
def test_normalize_domain_invalid(raw):
    with pytest.raises(sdf.SdfError) as excinfo:
        sdf.normalize_domain(raw)
    assert excinfo.value.exit_code == 2


# --- retry_delay -----------------------------------------------------------

def test_retry_delay_honors_retry_after():
    response = httpx.Response(429, headers={"Retry-After": "7"})
    assert sdf.retry_delay(response, 0) == 7.0


def test_retry_delay_caps_retry_after():
    response = httpx.Response(429, headers={"Retry-After": "3600"})
    assert sdf.retry_delay(response, 0) == sdf.MAX_DELAY


def test_retry_delay_backoff_range():
    response = httpx.Response(503)
    assert 1.0 <= sdf.retry_delay(response, 0) <= 1.5
    assert 2.0 <= sdf.retry_delay(response, 1) <= 2.5


# --- CLI -------------------------------------------------------------------

def test_lists_subdomains_on_stdout(api):
    api["responses"].append(json_response(GOOD_PAYLOAD))
    result = runner.invoke(sdf.app, ["--quiet", "example.com"])
    assert result.exit_code == 0
    assert result.output.splitlines() == GOOD_PAYLOAD["subdomains"]
    assert "domain=example.com" in str(api["requests"][0].url)


def test_summary_goes_to_stderr(api):
    api["responses"].append(json_response(GOOD_PAYLOAD))
    result = runner.invoke(sdf.app, ["example.com"])
    assert result.exit_code == 0
    assert "3 subdomain(s) found" in combined(result)
    for subdomain in GOOD_PAYLOAD["subdomains"]:
        assert subdomain in result.output


def test_empty_results_exit_0(api):
    api["responses"].append(json_response(EMPTY_PAYLOAD))
    result = runner.invoke(sdf.app, ["example.com"])
    assert result.exit_code == 0
    assert "No subdomains found" in combined(result)


def test_truncation_note_when_count_below_total(api):
    api["responses"].append(json_response(TRUNCATED_PAYLOAD))
    result = runner.invoke(sdf.app, ["example.com"])
    assert result.exit_code == 0
    assert "truncated" in combined(result)


def test_http_400_is_exit_1(api):
    api["responses"].append(httpx.Response(400, json={"error": "invalid domain"}))
    result = runner.invoke(sdf.app, ["example.com"])
    assert result.exit_code == 1
    assert "HTTP 400" in combined(result)


def test_invalid_domain_is_exit_2_without_request(api):
    result = runner.invoke(sdf.app, ["not a domain"])
    assert result.exit_code == 2
    assert api["requests"] == []


def test_retry_after_429_then_success(api):
    api["responses"] += [
        httpx.Response(429, headers={"Retry-After": "0"}),
        json_response(GOOD_PAYLOAD),
    ]
    result = runner.invoke(sdf.app, ["--quiet", "example.com"])
    assert result.exit_code == 0
    assert len(api["requests"]) == 2


def test_retries_exhausted_on_persistent_429(api):
    api["responses"] += [httpx.Response(429, headers={"Retry-After": "0"})] * 3
    result = runner.invoke(sdf.app, ["--quiet", "--retries", "2", "example.com"])
    assert result.exit_code == 1
    assert len(api["requests"]) == 3
    assert "HTTP 429" in combined(result)


def test_retry_on_503_then_success(api):
    api["responses"] += [httpx.Response(503), json_response(GOOD_PAYLOAD)]
    result = runner.invoke(sdf.app, ["--quiet", "example.com"])
    assert result.exit_code == 0
    assert len(api["requests"]) == 2


def test_connect_timeout_is_exit_1(api):
    api["responses"].append(httpx.ConnectTimeout("boom"))
    result = runner.invoke(sdf.app, ["--quiet", "example.com"])
    assert result.exit_code == 1
    assert "timed out" in combined(result)


def test_json_output(api):
    api["responses"].append(json_response(GOOD_PAYLOAD))
    result = runner.invoke(sdf.app, ["--quiet", "--json", "example.com"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["count"] == 3
    assert payload["subdomains"] == GOOD_PAYLOAD["subdomains"]


def test_csv_output(api):
    api["responses"].append(json_response(GOOD_PAYLOAD))
    result = runner.invoke(sdf.app, ["--quiet", "--csv", "example.com"])
    assert result.exit_code == 0
    assert result.output.splitlines() == ["subdomain"] + GOOD_PAYLOAD["subdomains"]


def test_count_only(api):
    api["responses"].append(json_response(GOOD_PAYLOAD))
    result = runner.invoke(sdf.app, ["--quiet", "--count-only", "example.com"])
    assert result.exit_code == 0
    assert result.output.strip() == "3"


def test_output_file_format_from_extension(api, tmp_path):
    api["responses"].append(json_response(GOOD_PAYLOAD))
    target = tmp_path / "subs.json"
    result = runner.invoke(sdf.app, ["--quiet", "--output", str(target), "example.com"])
    assert result.exit_code == 0
    assert result.output == ""
    assert json.loads(target.read_text(encoding="utf-8"))["count"] == 3


def test_output_txt_file(api, tmp_path):
    api["responses"].append(json_response(GOOD_PAYLOAD))
    target = tmp_path / "subs.txt"
    result = runner.invoke(sdf.app, ["--quiet", "-o", str(target), "example.com"])
    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8").splitlines() == GOOD_PAYLOAD["subdomains"]


def test_json_and_csv_conflict_is_exit_2(api):
    result = runner.invoke(sdf.app, ["--json", "--csv", "example.com"])
    assert result.exit_code == 2
    assert api["requests"] == []


def test_url_input_is_normalized(api):
    api["responses"].append(json_response(GOOD_PAYLOAD))
    result = runner.invoke(sdf.app, ["--quiet", "https://www.example.com/about"])
    assert result.exit_code == 0
    assert "domain=example.com" in str(api["requests"][0].url)


def test_broken_pipe_exits_0(api, monkeypatch):
    api["responses"].append(json_response(GOOD_PAYLOAD))
    monkeypatch.setattr(sdf.sys.stdout, "write", lambda *_: (_ for _ in ()).throw(BrokenPipeError()))
    result = runner.invoke(sdf.app, ["--quiet", "example.com"])
    assert result.exit_code == 0


def test_version():
    result = runner.invoke(sdf.app, ["--version"])
    assert result.exit_code == 0
    assert sdf.__version__ in result.output


if __name__ == "__main__":
    raise SystemExit(pytest.main(["-q", __file__]))
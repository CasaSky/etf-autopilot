import io
import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from etf_autopilot import capture
from etf_autopilot.cli import main
from etf_autopilot.domain import SafetyError, load_json
from etf_autopilot.history import validate_dataset

from .test_issuer import exports as exports


@pytest.fixture
def plan(exports):
    value = {
        "version": 1,
        "spec": load_json(exports / "spec.json"),
        "downloads": {
            "RESEARCH_EQUITY": {"provider": "ishares", "portfolio_id": "111"},
            "RESEARCH_BOND": {"provider": "ishares", "portfolio_id": "222"},
        },
    }
    path = exports / "capture-plan.json"
    path.write_text(json.dumps(value))
    return path


def fetcher(exports, calls):
    def fetch(url, limit, deadline):
        calls.append(url)
        parsed = urlparse(url)
        assert parsed.scheme == "https" and not parsed.username
        assert limit > 0 and deadline > 0
        if parsed.hostname == "www.ecb.europa.eu":
            path = exports / "fx.xml"
        else:
            assert parsed.hostname == "www.blackrock.com"
            product = parse_qs(parsed.query)["portfolioId"][0]
            path = exports / ("equity.xls" if product == "111" else "bond.xls")
        return path.read_bytes()

    return fetch


def test_capture_records_receipt_times_and_replays_without_network(plan, exports, tmp_path):
    calls = []
    ticks = iter(datetime(2026, 1, 9, tzinfo=UTC) + timedelta(seconds=i) for i in range(4))
    out = tmp_path / "captured"
    result = capture.capture_issuer(
        plan, out, fetch=fetcher(exports, calls), clock=lambda: next(ticks)
    )
    assert len(calls) == 3 and result["duplicate"] is False
    assert result["received_at"] == "2026-01-09T00:00:03+00:00"
    assert [item["received_at"] for item in result["sources"]] == [
        f"2026-01-09T00:00:0{i}+00:00" for i in range(1, 4)
    ]
    assert result["timing_evidence"] == "receipt_times_only_original_publication_times_unknown"
    assert result["execution_eligible"] is False and result["model_calls"] == 0
    dataset = load_json(out / "normalized/dataset.json")
    validate_dataset(dataset)
    assert dataset["manifest"]["retrieved_at"] == result["received_at"]
    assert out.stat().st_mode & 0o777 == 0o700

    def forbidden(*args):
        raise AssertionError("A completed capture must not read the network or clock")

    assert capture.capture_issuer(plan, out, fetch=forbidden, clock=forbidden) == {
        **result,
        "duplicate": True,
    }
    (out / "equity.xls").write_bytes(b"changed source")
    with pytest.raises(SafetyError, match="issuer_capture_file_changed"):
        capture.capture_issuer(plan, out, fetch=forbidden)


def test_incomplete_capture_cannot_be_reused_or_claim_completion(plan, exports, tmp_path):
    calls = []
    original = fetcher(exports, calls)

    def fail_second(url, limit, deadline):
        if calls:
            raise SafetyError("issuer_capture_network_failure")
        return original(url, limit, deadline)

    out = tmp_path / "failed"
    with pytest.raises(SafetyError, match="issuer_capture_network_failure"):
        capture.capture_issuer(plan, out, fetch=fail_second)
    assert not (out / "capture.json").exists()
    assert load_json(out / "capture.failed.json")["status"] == "incomplete_do_not_use_for_decisions"
    with pytest.raises(SafetyError, match="issuer_capture_incomplete_use_new_directory"):
        capture.capture_issuer(plan, out, fetch=original)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "ishares", "portfolio_id": "../private"},
        {"provider": "ishares", "portfolio_id": "111", "url": "https://example.invalid"},
        {"provider": "vanguard", "portfolio_id": "111"},
    ],
)
def test_plan_cannot_request_arbitrary_urls_or_providers(plan, tmp_path, change):
    value = load_json(plan)
    value["downloads"]["RESEARCH_EQUITY"] = change
    plan.write_text(json.dumps(value))
    calls = []
    with pytest.raises(SafetyError):
        capture.capture_issuer(plan, tmp_path / "blocked", fetch=lambda *args: calls.append(args))
    assert calls == [] and not (tmp_path / "blocked").exists()


def test_expired_capture_deadline_prevents_request(plan, tmp_path, monkeypatch):
    ticks = iter([0, 46])
    monkeypatch.setattr(capture.time, "monotonic", lambda: next(ticks))
    calls = []
    with pytest.raises(SafetyError, match="issuer_capture_deadline_exceeded"):
        capture.capture_issuer(plan, tmp_path / "expired", fetch=lambda *args: calls.append(args))
    assert calls == []


class Reply(io.BytesIO):
    status = 200


def test_network_attempt_is_bounded_and_errors_exclude_upstream_content(monkeypatch):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            raise HTTPError(request.full_url, 503, "private upstream content", {}, None)

    def factory(*handlers):
        assert handlers[0].proxies == {}
        assert isinstance(handlers[1], capture._NoRedirect)
        return Opener()

    monkeypatch.setattr(capture, "build_opener", factory)
    with pytest.raises(SafetyError, match="^issuer_capture_network_failure$"):
        capture._download("https://www.ecb.europa.eu/known.xml", 20, capture.time.monotonic() + 30)
    assert len(calls) == 1 and 0 < calls[0][1] <= 15
    assert set(dict(calls[0][0].header_items())) == {"User-agent"}
    assert (
        capture._NoRedirect().redirect_request(
            None, None, 302, None, None, "https://example.invalid"
        )
        is None
    )


def test_network_source_size_is_enforced_during_read(monkeypatch):
    class Opener:
        def open(self, *args, **kwargs):
            return Reply(b"too large")

    monkeypatch.setattr(capture, "build_opener", lambda *args: Opener())
    with pytest.raises(SafetyError, match="issuer_capture_source_size_limit"):
        capture._download("https://www.ecb.europa.eu/known.xml", 3, capture.time.monotonic() + 30)


def test_cli_outputs_capture_identity_without_raw_source_contents(monkeypatch, capsys):
    monkeypatch.setattr(
        capture,
        "capture_issuer",
        lambda *args: {
            "evidence_id": "e" * 64,
            "dataset_id": "d" * 64,
            "duplicate": False,
            "sources": [{}, {}, {}],
            "timing_evidence": "receipt_times_only_original_publication_times_unknown",
        },
    )
    assert main(["capture-issuer", "var/plan.json", "--out-dir", "var/capture"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sources"] == 3 and result["execution_eligible"] is False
    assert result["model_calls"] == 0 and "prices" not in result

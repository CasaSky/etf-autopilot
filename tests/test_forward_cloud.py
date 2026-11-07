from copy import deepcopy

import pytest

from etf_autopilot.capture import capture_issuer
from etf_autopilot.domain import SafetyError, digest, load_json
from etf_autopilot.forward import run_forward_cycle
from etf_autopilot.forward_cloud import MODEL_ID, STATE_KEY, run_cloud_tick
from etf_autopilot.forward_control import OwnedStore, trial_identity
from etf_autopilot.forward_selection import abandon_forward_selection
from etf_autopilot.forward_transfer import complete_transfer, freeze_transfer, install_transfer
from etf_autopilot.issuer import FX, SS
from etf_autopilot.observations import observations_for_capture
from etf_autopilot.store import LocalStore, S3Store

from .forward_s3_fixture import MemoryS3
from .test_capture import fetcher
from .test_forward import at
from .test_issuer import exports as exports
from .test_issuer import growth, xml_change
from .test_observations import captured as captured
from .test_observations import plan as plan

NOW = "2026-01-08T09:00:00Z"


def forbidden(*_):
    raise AssertionError("A replay must not invoke a provider or download sources")


@pytest.fixture
def cloud(config, captured, exports, tmp_path):
    plan_path, _, _ = captured

    def setup(*, declining=False):
        if declining:

            def change(root):
                for row in list(growth(root))[1:]:
                    stamp = row[0][0].text
                    if stamp and ("Dec 2025" in stamp or "Jan 2026" in stamp):
                        row[1].find(SS + "Data").text = "7,000.00"

            xml_change(exports / "equity.xls", change)
        config["operations"]["max_model_calls"] = 1
        capture_issuer(
            plan_path,
            tmp_path / "seed",
            fetch=fetcher(exports, []),
            clock=lambda: at("2026-01-08T00:00:00Z"),
        )
        data = observations_for_capture(plan_path, tmp_path / "seed")
        older = deepcopy(data)
        older["rows"] = [row for row in data["rows"] if row["date"] <= "2026-01-05"]
        older["observation_id"] = digest({k: v for k, v in older.items() if k != "observation_id"})
        local = LocalStore(tmp_path / "local.sqlite3")
        assert (
            run_forward_cycle(local, config, older, now=at("2026-01-08T07:00:00Z"))["status"]
            == "waiting_for_input"
        )
        client, bucket = MemoryS3(), "fixture-bucket"
        remote = S3Store(client, bucket, STATE_KEY)
        frozen = freeze_transfer(
            local, config, digest(local.read()), "aws", now=at("2026-01-08T07:01:00Z")
        )
        install_transfer(remote, config, frozen, now=at("2026-01-08T07:02:00Z"))
        complete_transfer(local, remote, config, frozen, now=at("2026-01-08T07:03:00Z"))
        value = load_json(plan_path)
        runtime = {
            "version": 1,
            "config_hash": digest(config),
            "plan_hash": digest(value),
            "trial_id": trial_identity(remote.read()),
            "model_enabled": True,
            "model_id": MODEL_ID,
            "model_cost_eur": "0.02",
            "capture_cost_eur": "0.002",
            "monthly_fixed_cost_eur": "0.30",
            "operating_cost_eur": "0.01",
        }
        return client, bucket, value, runtime, remote

    return setup


def run(cloud, config, exports, *, provider=forbidden, now=NOW, fetch=None, **kwargs):
    client, bucket, plan, runtime, _ = cloud
    return run_cloud_tick(
        client,
        bucket,
        config,
        plan,
        runtime,
        NOW,
        allow_model=True,
        provider=provider,
        fetch=fetch or fetcher(exports, []),
        clock=lambda: at(now),
        **kwargs,
    )


def test_cloud_model_sees_reservation_and_durable_capture_before_invocation(cloud, config, exports):
    setup = cloud(declining=True)
    client, bucket, _, _, remote = setup
    downloads, calls = [], []

    def provider(*_):
        state = remote.read()
        assert state["operations"]["months"]["2026-01"]["model_calls"] == 1
        assert state["operations"]["months"]["2026-01"]["spend_eur"] == "0.332"
        assert any(
            key.endswith("/ready.json")
            for owned_bucket, key in client.objects
            if owned_bucket == bucket
        )
        calls.append(1)
        return '{"strategy":"passive"}'

    first = run(setup, config, exports, provider=provider, fetch=fetcher(exports, downloads))
    assert first["selection_status"] == "ready" and first["orders"] == 0
    assert len(downloads) == 3 and calls == [1]
    before = remote.read()
    repeated = run(setup, config, exports, fetch=forbidden, now="2026-01-08T09:00:20Z")
    assert repeated["duplicate"] and repeated["receipt_id"] == first["receipt_id"]
    assert remote.read() == before and calls == [1]


def test_unknown_capture_reservation_never_repeats_an_external_attempt(cloud, config, exports):
    setup = cloud()
    client, _, _, _, remote = setup
    client.unknown_write_key = STATE_KEY
    with pytest.raises(SafetyError, match="state_write_unknown"):
        run(setup, config, exports, fetch=forbidden)
    result = run(setup, config, exports, fetch=forbidden)
    assert result["status"] == "awaiting_capture_evidence"
    bucket = remote.read()["operations"]["months"]["2026-01"]
    assert bucket["model_calls"] == 0 and bucket["spend_eur"] == "0.302"


def test_failed_capture_retains_one_cost_and_alert_without_retry(cloud, config, exports):
    setup = cloud()
    messages, downloads = [], []

    def broken(*_):
        downloads.append(1)
        raise SafetyError("fixture_source_unavailable")

    first = run(setup, config, exports, fetch=broken, notify=messages.append)
    assert first["status"] == "capture_failed" and first["notification"] == "sent"
    second = run(setup, config, exports, fetch=forbidden, notify=messages.append)
    assert second["status"] == "awaiting_capture_evidence"
    assert downloads == [1] and len(messages) == 1
    assert setup[-1].read()["operations"]["months"]["2026-01"]["spend_eur"] == "0.302"


@pytest.mark.parametrize("unknown_name", ["ready.json", "outcome.json"])
def test_unknown_artifact_ack_resumes_without_an_extra_paid_call(
    cloud, config, exports, unknown_name
):
    setup = cloud(declining=True)
    setup[0].unknown_write_key = f"paper/forward/ticks/2026-01-08/{unknown_name}"
    calls = []

    def provider(*_):
        calls.append(1)
        return '{"strategy":"passive"}'

    with pytest.raises(SafetyError, match="forward_artifact_write_unknown"):
        run(setup, config, exports, provider=provider)
    repeated = run(setup, config, exports, provider=provider, fetch=forbidden)
    assert repeated["selection_status"] == "ready" and calls == [1]
    assert setup[-1].read()["operations"]["months"]["2026-01"]["model_calls"] == 1


def test_worker_crash_keeps_unknown_model_reserved_and_alerts_on_replay(cloud, config, exports):
    setup = cloud(declining=True)

    class WorkerStopped(BaseException):
        pass

    def stopped(*_):
        raise WorkerStopped()

    with pytest.raises(WorkerStopped):
        run(setup, config, exports, provider=stopped)
    messages = []
    repeated = run(setup, config, exports, fetch=forbidden, notify=messages.append)
    assert repeated["selection_status"] == "reserved" and len(messages) == 1
    assert setup[-1].read()["operations"]["months"]["2026-01"]["model_calls"] == 1
    store = OwnedStore(setup[-1], "aws")
    before = store.read()
    decision_id = next(iter(before["decisions"]))
    with pytest.raises(SafetyError, match="forward_abandon_state_changed"):
        abandon_forward_selection(store, decision_id, config=config, expected_state_hash="0" * 64)
    assert store.read() == before
    abandon_forward_selection(store, decision_id, config=config, expected_state_hash=digest(before))
    assert store.read()["operations"] == before["operations"]
    assert store.read()["decisions"][decision_id]["status"] == "selection_abandoned"


def test_headroom_wait_retains_capture_then_resumes_from_evidence(cloud, config, exports):
    setup = cloud(declining=True)
    first = run(setup, config, exports, remaining_ms=lambda: 30_000)
    assert first["status"] == "awaiting_runtime_headroom"
    calls = []
    second = run(
        setup,
        config,
        exports,
        fetch=forbidden,
        remaining_ms=lambda: 180_000,
        provider=lambda *_: calls.append(1) or '{"strategy":"passive"}',
    )
    assert second["selection_status"] == "ready" and calls == [1]


def test_invalid_or_unapproved_runtime_fails_before_external_activity(cloud, config, exports):
    client, bucket, plan, runtime, _ = cloud()
    calls = deepcopy(client.calls)
    with pytest.raises(SafetyError, match="forward_model_permission_disabled"):
        run_cloud_tick(
            client, bucket, config, plan, runtime, NOW, clock=lambda: at(NOW), fetch=forbidden
        )
    assert client.calls == calls

    changed = {**runtime, "model_id": "unapproved-model"}
    with pytest.raises(SafetyError, match="forward_runtime_binding_or_cost_changed"):
        run_cloud_tick(
            client,
            bucket,
            config,
            plan,
            changed,
            NOW,
            allow_model=True,
            clock=lambda: at(NOW),
            fetch=forbidden,
        )
    with pytest.raises(SafetyError, match="forward_current_scheduled_tick_required"):
        run_cloud_tick(
            client,
            bucket,
            config,
            plan,
            runtime,
            "2026-01-07T09:00:00Z",
            allow_model=True,
            clock=lambda: at(NOW),
            fetch=forbidden,
        )
    assert client.calls == calls


def test_unknown_financial_result_ack_reads_saved_choice_without_reinvocation(
    cloud, config, exports
):
    setup = cloud(declining=True)
    calls = []

    def provider(*_):
        calls.append(1)
        setup[0].unknown_write_key = STATE_KEY
        return '{"strategy":"passive"}'

    with pytest.raises(SafetyError, match="state_write_unknown"):
        run(setup, config, exports, provider=provider)
    result = run(setup, config, exports, fetch=forbidden)
    assert result["selection_status"] == "ready" and calls == [1]
    assert setup[-1].read()["operations"]["months"]["2026-01"]["spend_eur"] == "0.332"


def test_cloud_executes_only_at_a_later_value_and_preserves_monthly_fixed_charge(
    cloud, config, exports
):
    setup = cloud(declining=True)
    calls = []
    first = run(
        setup,
        config,
        exports,
        provider=lambda *_: calls.append(1) or '{"strategy":"passive"}',
    )
    assert first["orders"] == 0

    def add_value(root):
        table = growth(root)
        row = deepcopy(table[1])
        row[0][0].text = "Fri, 09 Jan 2026"
        table.insert(1, row)

    for filename in ("equity.xls", "bond.xls"):
        xml_change(exports / filename, add_value)

    def add_fx(root):
        wrapper = root.find(FX + "Cube")
        point = deepcopy(wrapper[0])
        point.set("time", "2026-01-09")
        wrapper.insert(0, point)

    xml_change(exports / "fx.xml", add_fx)
    client, bucket, plan, runtime, remote = setup
    stamp = "2026-01-10T09:00:00Z"
    result = run_cloud_tick(
        client,
        bucket,
        config,
        plan,
        runtime,
        stamp,
        allow_model=True,
        clock=lambda: at(stamp),
        fetch=fetcher(exports, []),
        provider=forbidden,
    )
    assert result["status"] == "executed" and result["orders"] == 5 and calls == [1]
    state = remote.read()
    month = state["operations"]["months"]["2026-01"]
    assert month["spend_eur"] == "0.334" and month["fixed_runtime_spend_eur"] == "0.30"
    for account in state["accounts"].values():
        assert all(
            row["modeled_value_date"] == "2026-01-09"
            for row in account["ledger"]
            if row["type"] == "fill"
        )

import asyncio
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import ksefik.sync as sync_module
from ksefik.client import AuthError, ExportPackage, KsefError, RateLimitError
from ksefik.store import Store
from ksefik.sync import WINDOW, sync

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "sync.db")
    yield db
    db.close()


@pytest.fixture(autouse=True)
def frozen_time(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)

    monkeypatch.setattr(sync_module, "datetime", Clock)


def test_default_subjects_use_warsaw_midnight_and_persist_invoices(store):
    client = AsyncMock()
    client.export_invoices.side_effect = [
        ExportPackage(metadata=[{"ksefNumber": "A"}], xml={"A": b"<invoice/>"}, hwm=NOW),
        ExportPackage(hwm=NOW - timedelta(hours=1)),
    ]
    events = []
    result = asyncio.run(sync(client, store, date(2026, 10, 1), lambda *event: events.append(event)))
    assert result.added == {"issued": 1, "received": 0}
    assert result.total_added == 1
    assert store.get("A", "issued")["xml"] == b"<invoice/>"
    calls = client.export_invoices.call_args_list
    assert [call.args[:3] for call in calls] == [
        (subject, datetime(2026, 9, 30, 22, tzinfo=UTC), None) for subject in ("Subject1", "Subject2")
    ]
    assert store.checkpoint("Subject1") == NOW
    assert store.checkpoint("Subject2") == NOW - timedelta(hours=1)
    assert len(events) == 2


def test_historical_windows_resume_checkpoint_and_replay_is_idempotent(store):
    start = NOW - WINDOW - timedelta(days=1)
    store.set_checkpoint("Subject3", start)
    client = AsyncMock()
    client.export_invoices.side_effect = [
        ExportPackage(metadata=[{"ksefNumber": "A"}]),
        ExportPackage(metadata=[{"ksefNumber": "A"}, {"ksefNumber": "B"}], hwm=NOW),
    ]
    result = asyncio.run(sync(client, store, date(2020, 1, 1), subjects=["Subject3"]))
    assert result.added == {"third": 2}
    assert client.export_invoices.call_args_list[0].args[1:3] == (start, start + WINDOW)
    assert client.export_invoices.call_args_list[1].args[1:3] == (start + WINDOW, None)
    assert store.checkpoint("Subject3") == NOW


def test_truncated_package_resumes_at_last_storage_date(store):
    start = NOW - timedelta(days=5)
    resume = NOW - timedelta(days=2)
    store.set_checkpoint("SubjectAuthorized", start)
    client = AsyncMock()
    client.export_invoices.side_effect = [
        ExportPackage(is_truncated=True, last_permanent_storage=resume),
        ExportPackage(hwm=NOW),
    ]
    asyncio.run(sync(client, store, date(2026, 1, 1), subjects=["SubjectAuthorized"]))
    assert client.export_invoices.call_args_list[1].args[1] == resume
    assert store.checkpoint("SubjectAuthorized") == NOW


def test_failure_preserves_last_committed_window(store):
    start = NOW - WINDOW - timedelta(days=1)
    store.set_checkpoint("Subject1", start)
    client = AsyncMock()
    client.export_invoices.side_effect = [ExportPackage(), KsefError("offline")]
    with pytest.raises(KsefError, match="offline"):
        asyncio.run(sync(client, store, date(2026, 1, 1), subjects=["Subject1"]))
    assert store.checkpoint("Subject1") == start + WINDOW


@pytest.mark.parametrize("hwm", [None, NOW - timedelta(days=20)])
def test_checkpoint_never_moves_backwards(store, hwm):
    start = NOW - timedelta(days=1)
    store.set_checkpoint("Subject1", start)
    client = AsyncMock()
    client.export_invoices.return_value = ExportPackage(hwm=hwm)
    asyncio.run(sync(client, store, date(2026, 1, 1), subjects=["Subject1"]))
    assert store.checkpoint("Subject1") == start


@pytest.mark.parametrize("resume", [None, NOW - timedelta(days=2), NOW - timedelta(days=3)])
def test_truncation_without_forward_progress_stops_without_advancing_checkpoint(store, resume):
    start = NOW - timedelta(days=2)
    store.set_checkpoint("Subject1", start)
    client = AsyncMock()
    client.export_invoices.side_effect = [
        ExportPackage(is_truncated=True, last_permanent_storage=resume, hwm=NOW),
        AssertionError("sync repeated a non-advancing export"),
    ]
    with pytest.raises(KsefError, match="[Pp]ostęp|[Pp]ostep|[Tt]runc|[Uu]cięt"):
        asyncio.run(sync(client, store, date(2026, 1, 1), subjects=["Subject1"]))
    assert store.checkpoint("Subject1") == start


@pytest.mark.parametrize(
    "failure,continues",
    [
        (AuthError("Brak uprawnień", http_status=403), True),
        (KsefError("Ucięta paczka"), True),
        (AuthError("expired", http_status=401), False),
        (RateLimitError("wait", retry_after=5), False),
    ],
)
def test_failure_of_one_direction_does_not_block_the_others(store, failure, continues):
    received = {"ksefNumber": "R", "invoiceNumber": "FV/R", "issueDate": "2026-10-20"}

    async def export(subject, *args):
        if subject == "Subject1":
            raise failure
        return ExportPackage(metadata=[received], hwm=NOW)

    client = AsyncMock()
    client.export_invoices.side_effect = export
    with pytest.raises(KsefError) as caught:
        asyncio.run(sync(client, store, date(2026, 10, 1)))
    assert (store.get("R", "received") is not None) is continues
    if continues:
        assert "wystawione" in caught.value.message and store.checkpoint("Subject1") is None
    else:
        assert caught.value is failure


def test_each_direction_remembers_its_last_attempt(store):
    async def export(subject, *args):
        if subject == "Subject2":
            raise KsefError("Brak uprawnień do faktur otrzymanych.", http_status=403)
        return ExportPackage(hwm=NOW)

    client = AsyncMock()
    client.export_invoices.side_effect = export
    with pytest.raises(KsefError):
        asyncio.run(sync(client, store, date(2026, 10, 1)))
    assert store.sync_attempt("Subject1")["error"] is None
    failed = store.sync_attempt("Subject2")
    assert failed["error_kind"] == "permissions" and "Brak uprawnień" in failed["error"]
    client.export_invoices.side_effect = lambda *a, **k: ExportPackage(hwm=NOW)
    asyncio.run(sync(client, store, date(2026, 10, 1)))
    assert store.sync_attempt("Subject2")["error"] is None  # udana próba czyści błąd


def test_rate_limit_is_recorded_as_such(store):
    client = AsyncMock()
    client.export_invoices.side_effect = RateLimitError("wait", retry_after=30)
    with pytest.raises(RateLimitError):
        asyncio.run(sync(client, store, date(2026, 10, 1)))
    assert store.sync_attempt("Subject1")["error_kind"] == "rate_limit"


def test_backfill_fetches_older_windows_without_moving_the_checkpoint(store):
    store.set_checkpoint("Subject1", NOW)
    calls = []

    async def export(subject, start, end, *args):
        calls.append((subject, start, end))
        n = len(calls)
        return ExportPackage(metadata=[{"ksefNumber": f"K{n}", "invoiceNumber": f"FV/{n}", "issueDate": "2025-03-01"}])

    client = AsyncMock()
    client.export_invoices.side_effect = export
    result = asyncio.run(
        sync_module.backfill(client, store, date(2025, 1, 1), date(2025, 12, 31), subjects=["Subject1"])
    )
    assert store.checkpoint("Subject1") == NOW
    assert result.added == {"issued": len(calls)} and len(calls) == 5  # 365 dni w oknach po 90
    assert calls[0][1] == datetime(2024, 12, 31, 23, tzinfo=UTC)  # północ czasu polskiego
    assert calls[-1][2] == datetime(2025, 12, 31, 23, tzinfo=UTC)
    assert all(a[2] == b[1] for a, b in zip(calls, calls[1:], strict=False))  # okna bez przerw

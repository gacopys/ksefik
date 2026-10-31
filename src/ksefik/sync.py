"""Przyrostowa synchronizacja faktur z KSeF do lokalnej bazy (eksport paczek po dacie trwałego zapisu)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .client import AuthError, KsefClient, KsefError, Progress, RateLimitError, report
from .store import Store

WARSAW = ZoneInfo("Europe/Warsaw")
SUBJECTS = {"Subject1": "issued", "Subject2": "received", "Subject3": "third", "SubjectAuthorized": "authorized"}
LABELS = {"issued": "wystawione", "received": "otrzymane", "third": "jako podmiot 3", "authorized": "jako upoważniony"}
# KSeF dopuszcza zakres do 100 dni - zostawiamy margines.
WINDOW = timedelta(days=90)


def error_kind(exc: KsefError) -> str:
    """Rodzaj błędu do krótkiego opisu w pasku stanu."""
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    if exc.http_status == 403:
        return "permissions"
    if exc.http_status is None and exc.message.startswith("Brak połączenia"):
        return "connection"
    return "other"


@dataclass
class SyncResult:
    added: dict[str, int]

    @property
    def total_added(self) -> int:
        return sum(self.added.values())


async def sync(
    client: KsefClient,
    store: Store,
    since: date,
    progress: Progress | None = None,
    subjects: list[str] | None = None,
) -> SyncResult:
    """Pobiera przyrost faktur dla wskazanych typów podmiotu (domyślnie wystawione i otrzymane)."""
    subjects = subjects or ["Subject1", "Subject2"]
    added = {SUBJECTS[subject]: 0 for subject in subjects}
    initial = datetime.combine(since, time.min, tzinfo=WARSAW).astimezone(UTC)

    failures: list[tuple[str, KsefError]] = []
    for subject in subjects:
        direction = SUBJECTS[subject]
        try:
            added[direction] += await _sync_subject(client, store, subject, initial, progress)
        except (RateLimitError, AuthError) as exc:
            store.record_sync_attempt(subject, exc.message, error_kind(exc))
            if isinstance(exc, RateLimitError) or exc.http_status != 403:
                raise  # limit zapytań albo nieważne logowanie dotyczą wszystkich rodzajów faktur
            failures.append((direction, exc))
        except KsefError as exc:
            # Błąd jednego rodzaju faktur (np. brak uprawnień do otrzymanych) nie wstrzymuje pozostałych.
            store.record_sync_attempt(subject, exc.message, error_kind(exc))
            failures.append((direction, exc))
        else:
            store.record_sync_attempt(subject)
    if failures:
        message = "\n".join(f"Faktury {LABELS[d]}: {exc.message}" for d, exc in failures)
        details = [detail for _, exc in failures for detail in exc.details]
        raise KsefError(message, http_status=failures[0][1].http_status, details=details) from failures[0][1]
    return SyncResult(added=added)


async def _sync_subject(
    client: KsefClient, store: Store, subject: str, initial: datetime, progress: Progress | None
) -> int:
    """Pobiera przyrost faktur jednego rodzaju. Zwraca liczbę nowych faktur."""
    direction = SUBJECTS[subject]
    added = 0
    start = store.checkpoint(subject) or initial
    while True:
        now = datetime.now(UTC)
        window_end = start + WINDOW
        date_to = None if window_end >= now else window_end
        await report(progress, "export", f"Faktury {LABELS[direction]} od {start.astimezone(WARSAW):%d.%m.%Y}…")
        package = await client.export_invoices(subject, start, date_to, progress)
        added += store.upsert_metadata(direction, package.metadata, package.xml)

        if package.is_truncated:
            if not package.last_permanent_storage or package.last_permanent_storage <= start:
                raise KsefError(
                    "Ucięta paczka faktur nie pozwala na postęp synchronizacji. "
                    "Punkt wznowienia pozostaje bez zmian; spróbuj ponownie później."
                )
            start = package.last_permanent_storage
            store.set_checkpoint(subject, start)
            continue
        if date_to is not None:
            store.set_checkpoint(subject, date_to)
            start = date_to
            continue
        # Najświeższe okno: następnym razem zaczynamy od HWM (dane powyżej HWM mogą się jeszcze uzupełnić).
        hwm = package.hwm if package.hwm and package.hwm > start else start
        store.set_checkpoint(subject, hwm)
        return added


async def backfill(
    client: KsefClient,
    store: Store,
    date_from: date,
    date_to: date,
    progress: Progress | None = None,
    subjects: list[str] | None = None,
) -> SyncResult:
    """Pobiera faktury z okresu sprzed synchronizacji (np. starsze niż `synchronizuj_od`).

    Nie przesuwa punktu wznowienia - zwykła synchronizacja dalej zaczyna tam, gdzie skończyła.
    """
    subjects = subjects or ["Subject1", "Subject2"]
    added = {SUBJECTS[subject]: 0 for subject in subjects}
    start_all = datetime.combine(date_from, time.min, tzinfo=WARSAW).astimezone(UTC)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=WARSAW).astimezone(UTC)
    for subject in subjects:
        direction = SUBJECTS[subject]
        start = start_all
        while start < end:
            window_end = min(start + WINDOW, end)
            label = f"Starsze faktury {LABELS[direction]}: {start.astimezone(WARSAW):%d.%m.%Y}…"
            await report(progress, "export", label)
            package = await client.export_invoices(subject, start, window_end, progress)
            added[direction] += store.upsert_metadata(direction, package.metadata, package.xml)
            if package.is_truncated:
                if not package.last_permanent_storage or package.last_permanent_storage <= start:
                    raise KsefError("Ucięta paczka faktur nie pozwala na postęp pobierania - spróbuj ponownie później.")
                start = package.last_permanent_storage
                continue
            start = window_end
    return SyncResult(added=added)

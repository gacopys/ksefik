"""Wspólne narzędzia testów interfejsu i zrzutów ekranu.

Profil z profile.example.toml w katalogu tymczasowym, baza z przykładowymi fakturami, sztuczny klient KSeF
(AsyncMock z rozsądnymi odpowiedziami) oraz sprawdzanie, czy kontrolka mieści się na ekranie.
Używane przez tests/ui/ i scripts/screenshot.py.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

from textual.widget import Widget

from ksefik.client import ExportPackage
from ksefik.invoice import Line, build_xml, new_draft
from ksefik.profile import Party, Profile, load_profile
from ksefik.store import Store
from ksefik.submissions import metadata_from_xml
from ksefik.ui.app import KsefApp

ROOT = Path(__file__).resolve().parents[2]
SIZES = [(80, 24), (120, 40), (160, 50)]

SUPPLIERS = [
    Party(nazwa="Biuro Rachunkowe Bilans Sp. z o.o.", nip="5260250274", adres_l1="ul. Polna 3", adres_l2="Kraków"),
    Party(nazwa="Hosting Polska S.A.", nip="7010001454", adres_l1="ul. Serwerowa 10", adres_l2="Gdańsk"),
    Party(nazwa="Papiernik Jan Nowak", nip="1132191233", adres_l1="ul. Krótka 2", adres_l2="Łódź"),
]


def make_profile(folder: Path) -> Profile:
    path = folder / "profile.toml"
    shutil.copy(ROOT / "profile.example.toml", path)
    return load_profile(path)


def fake_client() -> AsyncMock:
    """Klient KSeF, który odpowiada pustymi listami (testy ustawiają konkretne odpowiedzi same)."""
    fake = AsyncMock()
    fake.auth_reference = "AUTH"
    for method in (
        "list_sessions",
        "session_invoices",
        "list_tokens",
        "auth_sessions",
        "query_certificates",
        "query_person_permissions",
        "query_personal_permissions",
        "query_authorizations",
        "query_entity_permissions",
        "query_subunit_permissions",
        "query_eu_entity_permissions",
        "query_entity_roles",
        "query_subordinate_entities",
        "query_collective_identifiers",
        "peppol_providers",
    ):
        getattr(fake, method).return_value = []
    fake.certificate_limits.return_value = {"canRequest": True, "enrollment": {}, "certificate": {}}
    fake.context_limits.return_value = {}
    fake.subject_limits.return_value = {}
    fake.rate_limits.return_value = {}
    fake.fetch_upo.return_value = None
    # Synchronizacja bez nowych faktur: punkt wznowienia przesuwa się na „teraz”.
    fake.export_invoices.side_effect = lambda *a, **k: ExportPackage(hwm=datetime.now(UTC))
    return fake


def _metadata(xml: bytes, ksef_number: str, direction: str) -> dict:
    meta = metadata_from_xml(xml, ksef_number)
    meta["acquisitionDate"] = meta["issueDate"] + "T12:00:00+00:00"
    return meta


def seed_store(store: Store, profile: Profile, today: date | None = None) -> None:
    """Kilkanaście faktur wystawionych i otrzymanych z bieżącego roku (z treścią XML)."""
    today = today or date.today()
    contractors = [c.party for c in profile.contractors]
    items = list(profile.items)
    numbers: list[str] = []
    for n in range(12):
        day = today - timedelta(days=9 * n + 1)
        buyer = contractors[n % len(contractors)]
        contractor = profile.contractor(buyer)
        item = profile.default_item(contractor) or items[0]
        draft = new_draft(profile, day, numbers, buyer=buyer, item=item, net=Decimal(1500 + 730 * n))
        if n == 4:
            extra = Line(name="Konsultacje", quantity=Decimal(6), unit="godz.", unit_price=Decimal(250))
            draft = draft.with_changes(lines=(*draft.lines, extra))
        if draft.currency != "PLN":
            draft = draft.with_changes(exchange_rate=Decimal("4.2512"), exchange_rate_info="tabela NBP")
        numbers.append(draft.number)
        xml = build_xml(draft)
        ksef = f"{profile.seller.nip}-{day:%Y%m%d}-{0xA10000 + n:06X}-{0xB20000 + n:06X}-{n:02X}"
        store.upsert_metadata("issued", [_metadata(xml, ksef, "issued")], {ksef: xml})
    for n in range(8):
        day = today - timedelta(days=11 * n + 3)
        supplier = SUPPLIERS[n % len(SUPPLIERS)]
        seller_profile = replace(profile, seller=supplier, contractors=(), items=())
        draft = new_draft(seller_profile, day, [], buyer=profile.seller, net=Decimal(240 + 95 * n))
        draft = draft.with_changes(
            number=f"FS/{n + 1}/{day:%m}/{day:%Y}",
            lines=(
                Line(
                    name=["Prowadzenie księgowości", "Hosting serwera", "Materiały biurowe"][n % 3],
                    unit_price=Decimal(240 + 95 * n),
                ),
            ),
        )
        xml = build_xml(draft)
        ksef = f"{supplier.nip}-{day:%Y%m%d}-{0xC30000 + n:06X}-{0xD40000 + n:06X}-{n:02X}"
        store.upsert_metadata("received", [_metadata(xml, ksef, "received")], {ksef: xml})
    for subject in ("Subject1", "Subject2"):
        store.set_checkpoint(subject, datetime.now(UTC) - timedelta(minutes=5))


class HarnessApp(KsefApp):
    """Aplikacja bez automatycznego startu: testy i zrzuty same wybierają ekran."""

    CSS_PATH = ROOT / "src/ksefik/ui/app.tcss"

    def __init__(self, *args, start: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.start_flow = start

    def on_mount(self, event) -> None:
        # Textual wywołuje on_mount każdej klasy w MRO - bez prevent_default ruszyłby normalny start aplikacji.
        if not self.start_flow:
            event.prevent_default()
            self._use_profile_theme()


def build_app(
    folder: Path, *, seed: bool = True, profile: Profile | None = None, start: bool = False
) -> tuple[HarnessApp, AsyncMock, Store]:
    profile = profile or make_profile(folder)
    store = Store(folder / "ui.db")
    if seed:
        seed_store(store, profile)
    fake = fake_client()
    app = HarnessApp(profile, profile_path=profile.path, store=store, start=start)

    async def get_client():
        app.client = fake
        return fake

    app.client = fake  # zalogowany jak po starcie z zapisanym tokenem
    app.get_client = get_client  # type: ignore[method-assign]
    app.make_client = lambda credentials: fake  # type: ignore[method-assign]
    return app, fake, store


# ------------------------------------------------------------------- widoczność


def visible_region(widget: Widget):
    """Część kontrolki faktycznie widoczna na ekranie (po przycięciu przez przewijane kontenery)."""
    geometry = widget.screen.find_widget(widget)
    return geometry.region.intersection(geometry.clip)


def assert_visible(widget: Widget, what: str = "") -> None:
    """Kontrolka jest w całości widoczna: nie wystaje poza ekran i nie jest ukryta pod krawędzią przewijania."""
    name = what or f"{type(widget).__name__}#{widget.id}"
    assert widget.display and widget.visible, f"{name} jest ukryta"
    region = widget.screen.find_widget(widget).region
    shown = visible_region(widget)
    assert region.area > 0, f"{name} nie ma rozmiaru"
    assert shown == region, f"{name} widoczna tylko częściowo: {shown} z {region} (ekran {widget.screen.size})"
    assert widget.screen.region.contains_region(region), f"{name} poza ekranem {widget.screen.size}: {region}"

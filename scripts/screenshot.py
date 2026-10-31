"""Zrzuty ekranów ksefik (SVG) w rozmiarach 80×24, 120×40 i 160×50 - do przeglądu wyglądu.

    python scripts/screenshot.py --out KATALOG [--size 80x24 ...] [nazwa ...]
    python scripts/screenshot.py --list

Bez nazw renderuje wszystkie ekrany. Dane są fikcyjne (profile.example.toml i przykładowe faktury),
a połączenie z KSeF jest udawane. PNG z tych plików robi `make screens` (headless Firefox).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
Scenario = Callable[..., Awaitable[None]]
SCENARIOS: dict[str, Scenario] = {}


def scenario(name: str) -> Callable[[Scenario], Scenario]:
    def register(func: Scenario) -> Scenario:
        SCENARIOS[name] = func
        return func

    return register


async def settle(pilot) -> None:
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


# ------------------------------------------------------------------- scenariusze


@scenario("lista")
async def _list(app, pilot, fake) -> None:
    from ksefik.ui.main_screen import MainScreen

    await app.push_screen(MainScreen())
    await settle(pilot)


@scenario("lista-zle-daty")
async def _list_bad_dates(app, pilot, fake) -> None:
    from ksefik.ui.main_screen import MainScreen

    screen = MainScreen()
    await app.push_screen(screen)
    await settle(pilot)
    screen.query_one("#date-from").value = "31.12.2026"
    screen.query_one("#date-to").value = "01.01.2026"
    await settle(pilot)


@scenario("lista-brak-wynikow")
async def _list_no_results(app, pilot, fake) -> None:
    from ksefik.ui.main_screen import MainScreen

    screen = MainScreen()
    await app.push_screen(screen)
    await settle(pilot)
    screen.query_one("#search").value = "nieistniejacy"
    await pilot.pause(0.4)
    await settle(pilot)


@scenario("lista-lokalnie")
async def _list_local(app, pilot, fake) -> None:
    from ksefik.ui.main_screen import MainScreen

    app.work_locally()
    await app.push_screen(MainScreen())
    await settle(pilot)


@scenario("lista-pusta")
async def _list_empty(app, pilot, fake) -> None:
    from ksefik.ui.main_screen import MainScreen

    app.store.db.execute("DELETE FROM invoices")
    await app.push_screen(MainScreen())
    await settle(pilot)


@scenario("szkice")
async def _drafts(app, pilot, fake) -> None:
    from dataclasses import replace

    from ksefik.ui.main_screen import MainScreen

    first = _draft(app)
    app.save_draft(first)
    eu = app.profile.contractors[1].party
    second = replace(first, number="FV/5/10/2026", buyer=eu, currency="EUR")
    app.save_draft(second, fields={"number": "FV/5/10/2026", "buyer_name": eu.nazwa, "currency": "EUR"})
    screen = MainScreen()
    await app.push_screen(screen)
    await settle(pilot)
    screen.query_one("#direction-tabs").active = "drafts"
    await settle(pilot)


@scenario("faktura")
async def _invoice(app, pilot, fake) -> None:
    from ksefik.ui.invoice_screens import InvoiceScreen

    row = app.store.db.execute("SELECT * FROM invoices WHERE direction = 'issued' ORDER BY issue_date DESC").fetchone()
    await app.push_screen(InvoiceScreen(row))
    await settle(pilot)


@scenario("faktura-korekta")
async def _invoice_correction(app, pilot, fake) -> None:
    from datetime import date

    from ksefik.invoice import as_correction, build_xml
    from ksefik.submissions import metadata_from_xml
    from ksefik.ui.invoice_screens import InvoiceScreen

    xml = build_xml(as_correction(_draft(app), "K0", app.profile, date.today(), []))
    ksef = f"{app.profile.seller.nip}-20261024-0A1B2C-3D4E5F-77"
    app.store.upsert_metadata("issued", [metadata_from_xml(xml, ksef)], {ksef: xml})
    await app.push_screen(InvoiceScreen(app.store.get(ksef, "issued")))
    await settle(pilot)


@scenario("braki-kopii")
async def _reuse_gaps(app, pilot, fake) -> None:
    from ksefik.invoice import build_xml
    from ksefik.submissions import metadata_from_xml
    from ksefik.ui.invoice_screens import InvoiceScreen

    xml = build_xml(_draft(app)).replace(b"<P_15>", b"<WZ>WZ/7/2026</WZ><P_15>", 1)
    ksef = f"{app.profile.seller.nip}-20261024-0A1B2C-3D4E5F-78"
    app.store.upsert_metadata("issued", [metadata_from_xml(xml, ksef)], {ksef: xml})
    await app.push_screen(InvoiceScreen(app.store.get(ksef, "issued")))
    await settle(pilot)
    await pilot.press("d")
    await settle(pilot)


@scenario("szybka-faktura")
async def _quick(app, pilot, fake) -> None:
    from ksefik.ui.invoice_screens import QuickInvoiceScreen
    from ksefik.ui.main_screen import MainScreen

    await app.push_screen(MainScreen())
    await settle(pilot)
    await app.push_screen(QuickInvoiceScreen())
    await settle(pilot)
    await pilot.press(*"4200")
    await settle(pilot)


@scenario("szybka-zmiana")
async def _quick_switch(app, pilot, fake) -> None:
    from ksefik.ui.invoice_screens import QuickInvoiceScreen
    from ksefik.ui.main_screen import MainScreen

    await app.push_screen(MainScreen())
    await settle(pilot)
    screen = QuickInvoiceScreen()
    await app.push_screen(screen)
    await settle(pilot)
    screen.query_one("#buyer").value = app.profile.contractors[1].party.key  # Beispiel GmbH: EUR, inna pozycja
    await settle(pilot)


@scenario("faktura-eur")
async def _invoice_eur(app, pilot, fake) -> None:
    from ksefik.ui.invoice_screens import InvoiceScreen

    row = app.store.db.execute("SELECT * FROM invoices WHERE currency = 'EUR' ORDER BY issue_date DESC").fetchone()
    screen = InvoiceScreen(row)
    await app.push_screen(screen)
    await settle(pilot)
    screen.query_one("#document").scroll_end(animate=False)
    await settle(pilot)


@scenario("podsumowanie-zaplacona")
async def _summary_paid(app, pilot, fake) -> None:
    from dataclasses import replace
    from datetime import date

    from ksefik.ui.invoice_screens import SummaryScreen

    draft = _draft(app)
    draft = draft.with_changes(payment=replace(draft.payment, paid=True, paid_date=date.today(), due_date=None))
    screen = SummaryScreen(draft)
    await app.push_screen(screen)
    await settle(pilot)
    screen.query_one("#document").scroll_end(animate=False)
    await settle(pilot)


def _draft(app):
    from datetime import date
    from decimal import Decimal

    from ksefik.invoice import new_draft

    return new_draft(app.profile, date.today(), app.store.reserved_numbers(), net=Decimal(4200))


@scenario("edycja")
async def _editor(app, pilot, fake) -> None:
    from ksefik.ui.editor import EditScreen

    await app.push_screen(EditScreen(_draft(app)))
    await settle(pilot)


@scenario("pozycja")
async def _line(app, pilot, fake) -> None:
    from ksefik.ui.editor import EditScreen, LineScreen

    draft = _draft(app)
    await app.push_screen(EditScreen(draft))
    await settle(pilot)
    await app.push_screen(LineScreen(draft.lines[0]))
    await settle(pilot)


@scenario("podsumowanie")
async def _summary(app, pilot, fake) -> None:
    from ksefik.ui.invoice_screens import SummaryScreen

    await app.push_screen(SummaryScreen(_draft(app)))
    await settle(pilot)


@scenario("wysylka-przyjeta")
async def _send_ok(app, pilot, fake) -> None:
    from datetime import datetime

    from ksefik.client import SendResult
    from ksefik.invoice import build_xml
    from ksefik.ui.invoice_screens import SendScreen

    nip = app.profile.seller.nip
    fake.send_invoice.return_value = SendResult(
        f"{nip}-20261024-0A1B2C-3D4E5F-6A", "20261024-SO-1", "20261024-EE-1", datetime.now(UTC), b"<UPO/>"
    )
    draft = _draft(app)
    await app.push_screen(SendScreen(draft, build_xml(draft)))
    await settle(pilot)


@scenario("faktura-link")
async def _invoice_link(app, pilot, fake) -> None:
    await _invoice(app, pilot, fake)
    app.screen.query_one("#document").scroll_end(animate=False)
    await settle(pilot)


@scenario("kopiuj-recznie")
async def _copy_fallback(app, pilot, fake) -> None:
    from ksefik.ui import app as app_module

    app_module.system_clipboard = lambda text: False  # np. przez SSH: schowek nie potwierdza kopiowania
    await _invoice(app, pilot, fake)
    await pilot.press("l")
    await settle(pilot)


@scenario("zapis-blad")
async def _save_error(app, pilot, fake) -> None:
    from ksefik.ui import app as app_module

    locked = Path(tempfile.mkdtemp())
    locked.chmod(0o500)
    app_module.downloads_dir = lambda: locked
    await _invoice(app, pilot, fake)
    await pilot.press("x")
    await settle(pilot)


@scenario("wysylka-nieznana")
async def _send_unknown(app, pilot, fake) -> None:
    from ksefik.client import OutcomeUnknown
    from ksefik.invoice import build_xml
    from ksefik.ui.invoice_screens import SendScreen

    async def send(xml, progress, on_refs=None):
        for step in ("auth", "session"):
            progress(step, "start")
            progress(step, "ok")
        progress("upload", "start")
        raise OutcomeUnknown("20261024-SO-1")

    fake.send_invoice.side_effect = send
    draft = _draft(app)
    await app.push_screen(SendScreen(draft, build_xml(draft)))
    await settle(pilot)


@scenario("wysylka-odrzucona")
async def _send_rejected(app, pilot, fake) -> None:
    from ksefik.client import InvoiceRejected
    from ksefik.invoice import build_xml
    from ksefik.ui.invoice_screens import SendScreen

    fake.send_invoice.side_effect = InvoiceRejected(
        "KSeF odrzucił fakturę: Błąd weryfikacji semantyki dokumentu (kod 450)",
        code=450,
        details=["Nieprawidłowy NIP nabywcy."],
    )
    draft = _draft(app)
    await app.push_screen(SendScreen(draft, build_xml(draft)))
    await settle(pilot)


@scenario("wysylka-niewyslana")
async def _send_not_submitted(app, pilot, fake) -> None:
    from ksefik.client import NotSubmitted
    from ksefik.invoice import build_xml
    from ksefik.ui.invoice_screens import SendScreen

    fake.send_invoice.side_effect = NotSubmitted("Brak połączenia z KSeF (ConnectError). Sprawdź internet.")
    draft = _draft(app)
    await app.push_screen(SendScreen(draft, build_xml(draft)))
    await settle(pilot)


@scenario("import")
async def _import(app, pilot, fake) -> None:
    from ksefik.invoice import build_xml
    from ksefik.ui.import_screen import ImportScreen

    folder = Path(tempfile.mkdtemp())
    draft = _draft(app)
    for n in range(3):
        (folder / f"faktura-{n + 1}.xml").write_bytes(build_xml(draft.with_changes(number=f"IMP/{n + 1}/2026")))
    (folder / "zepsuta.xml").write_text("<Faktura>", encoding="utf-8")
    screen = ImportScreen()
    await app.push_screen(screen)
    await settle(pilot)
    screen.query_one("#import-path").value = str(folder)
    screen._load()
    await settle(pilot)


@scenario("import-historia")
async def _import_history(app, pilot, fake) -> None:
    from ksefik.invoice import build_xml
    from ksefik.ui.import_screen import ImportScreen

    draft = _draft(app)
    for n, status in enumerate(["accepted", "processing", "not_submitted"], 1):
        xml = build_xml(draft.with_changes(number=f"IMP/{n}/2026"))
        app.store.record_sent(
            invoice_number=f"IMP/{n}/2026",
            status=status,
            xml=xml,
            file_name=f"faktura-{n}.xml",
            source="import",
            session_ref="20261024-SO-1",
            ksef_number=f"{app.profile.seller.nip}-20261024-0A1B2C-3D4E5F-0{n}" if status == "accepted" else None,
            message="Brak połączenia z KSeF" if status == "not_submitted" else None,
        )
    await app.push_screen(ImportScreen())
    await settle(pilot)


@scenario("token")
async def _token(app, pilot, fake) -> None:
    from ksefik.ui.setup_screen import TokenScreen

    await app.push_screen(TokenScreen(first_run=True))
    await settle(pilot)


@scenario("kreator")
async def _wizard(app, pilot, fake) -> None:
    from ksefik.ui.setup_screen import profile_wizard

    await app.push_screen(profile_wizard(app.profile_path))
    await settle(pilot)


async def _fake_registry(nip):
    from ksefik.registry import CompanyData

    accounts = ("3" * 26,) * 4
    return CompanyData(nip, "ZIELONY OGRÓD SP. Z O.O.", "OGRODOWA 12/3", "00-950 WARSZAWA", "Czynny", accounts)


@scenario("kreator-nip")
async def _wizard_nip(app, pilot, fake) -> None:
    from ksefik import registry
    from ksefik.ui.setup_screen import profile_wizard

    registry.find_company = _fake_registry
    form = profile_wizard(app.profile_path)
    await app.push_screen(form)
    await settle(pilot)
    form.query_one("#f-nip").value = "9512345675"
    form.query_one("#act-nip").press()
    await settle(pilot)


@scenario("edycja-nip")
async def _editor_nip(app, pilot, fake) -> None:
    from dataclasses import replace

    from ksefik import registry
    from ksefik.profile import Party
    from ksefik.ui.editor import EditScreen

    registry.find_company = _fake_registry
    editor = EditScreen(replace(_draft(app), buyer=Party()))
    await app.push_screen(editor)
    await settle(pilot)
    editor.query_one("#buyer_id").value = "9512345675"
    editor.query_one("#buyer_lookup").press()
    await settle(pilot)
    editor.query_one("#buyer_id").focus()
    await settle(pilot)


@scenario("kreator-2")
async def _wizard_2(app, pilot, fake) -> None:
    from ksefik.ui.setup_screen import profile_wizard

    await app.push_screen(profile_wizard(app.profile_path, {"nip": "5438539502"}, step=2))
    await settle(pilot)


@scenario("wybor")
async def _choice(app, pilot, fake) -> None:
    from ksefik.ui.permissions import PermissionsScreen

    screen = PermissionsScreen()
    await app.push_screen(screen)
    await settle(pilot)
    screen.action_grant()
    await settle(pilot)


@scenario("maly-terminal")
async def _too_small(app, pilot, fake) -> None:
    from ksefik.ui.main_screen import MainScreen

    await app.push_screen(MainScreen())
    await settle(pilot)
    await pilot.resize_terminal(70, 20)
    await settle(pilot)


@scenario("konto")
async def _account(app, pilot, fake) -> None:
    from ksefik.ui.account import AccountScreen

    await app.push_screen(AccountScreen())
    await settle(pilot)


@scenario("sesje")
async def _sessions(app, pilot, fake) -> None:
    from ksefik.ui.account import SessionsScreen

    fake.list_sessions.return_value = [
        {
            "referenceNumber": f"20261024-SO-{n:04X}",
            "dateCreated": f"2026-10-2{n}T10:00:00Z",
            "dateUpdated": f"2026-10-2{n}T10:01:00Z",
            "status": {"code": 200, "description": "Sesja przetworzona"},
            "successfulInvoiceCount": 1,
            "totalInvoiceCount": 1,
            "failedInvoiceCount": 0,
        }
        for n in range(1, 5)
    ]
    await app.push_screen(SessionsScreen())
    await settle(pilot)


@scenario("formularz")
async def _form(app, pilot, fake) -> None:
    from ksefik.ui.permissions import PermissionsScreen

    screen = PermissionsScreen()
    await app.push_screen(screen)
    await settle(pilot)
    form, _ = screen._person_form()
    await app.push_screen(form)
    await settle(pilot)


def _sent_ledger(app) -> None:
    from ksefik.invoice import build_xml

    store = app.store
    rows = store.db.execute("SELECT * FROM invoices WHERE direction = 'issued' ORDER BY issue_date LIMIT 3").fetchall()
    for row in rows:
        store.record_sent(
            invoice_number=row["invoice_number"],
            status="accepted",
            ksef_number=row["ksef_number"],
            session_ref="20261024-SO-1A2B3C4D5E",
            invoice_ref="20261024-EE-1A2B3C4D5E",
            upo=b"<UPO/>",
            upo_state="ready",
        )
    draft = _draft(app)
    store.record_sent(
        invoice_number="FV/5/10/2026",
        status="processing",
        xml=build_xml(draft),
        session_ref="20261024-SO-9F8E7D6C5B",
        invoice_ref="20261024-EE-9F8E7D6C5B",
    )
    store.record_sent(
        invoice_number="import-7.xml",
        status="rejected",
        message="KSeF odrzucił fakturę: Nieprawidłowy NIP nabywcy (kod 450).",
        file_name="import-7.xml",
        source="import",
    )


@scenario("akcje")
async def _actions(app, pilot, fake) -> None:
    from ksefik.ui.main_screen import MainScreen

    _sent_ledger(app)
    await app.push_screen(MainScreen())
    await settle(pilot)
    await pilot.press("a")
    await settle(pilot)


@scenario("lista-wysylki")
async def _list_waiting(app, pilot, fake) -> None:
    from ksefik.ui.main_screen import MainScreen

    _sent_ledger(app)
    await app.push_screen(MainScreen())
    await settle(pilot)


@scenario("wysylki")
async def _sent(app, pilot, fake) -> None:
    from ksefik.ui.sent_screen import SentScreen

    _sent_ledger(app)
    await app.push_screen(SentScreen())
    await settle(pilot)


@scenario("pomoc")
async def _help(app, pilot, fake) -> None:
    from ksefik.ui.main_screen import MainScreen

    await app.push_screen(MainScreen())
    await settle(pilot)
    await pilot.press("f1")
    await settle(pilot)


@scenario("synchronizacja-blad")
async def _sync_error(app, pilot, fake) -> None:
    from datetime import datetime, timedelta

    from ksefik.ui.main_screen import MainScreen

    app.store.db.execute("DELETE FROM invoices WHERE direction = 'received'")
    app.store.db.commit()
    app.store.record_sync_attempt(
        "Subject2", "Brak uprawnień do przeglądania faktur otrzymanych (InvoiceRead).", "permissions"
    )
    app.sync_blocked_until = datetime.now(UTC) + timedelta(minutes=10)
    screen = MainScreen()
    await app.push_screen(screen)
    await settle(pilot)
    screen.query_one("#direction-tabs").active = "received"
    await settle(pilot)


@scenario("zmiana-profilu")
async def _profile_switch(app, pilot, fake) -> None:
    from ksefik.ui.main_screen import MainScreen

    await app.push_screen(MainScreen())
    await settle(pilot)
    path = app.profile.path
    path.write_text(path.read_text().replace('srodowisko = "prod"', 'srodowisko = "test"'))
    app.reload_profile()
    await settle(pilot)


@scenario("starsze-faktury")
async def _backfill(app, pilot, fake) -> None:
    from ksefik.ui.main_screen import MainScreen

    screen = MainScreen()
    await app.push_screen(screen)
    await settle(pilot)
    screen.action_backfill()
    await settle(pilot)


@scenario("wyloguj")
async def _logout(app, pilot, fake) -> None:
    from ksefik.ui.main_screen import MainScreen

    await app.push_screen(MainScreen())
    await settle(pilot)
    app.logout()
    await settle(pilot)


@scenario("nowy-token")
async def _new_token(app, pilot, fake) -> None:
    from ksefik.ui.account import TokenCreatedScreen

    fake.wait_for_token.return_value = {"status": "Active"}
    token = "20261024-EC-2A1B3C4D5E-6F7A8B9C0D-1E|nip-5438539502|" + "a1b2c3d4" * 8
    await app.push_screen(TokenCreatedScreen(token, "Program księgowy w biurze", "20261024-EC-2A1B3C4D5E"))
    await settle(pilot)


@scenario("uprawnienia-posrednie")
async def _indirect(app, pilot, fake) -> None:
    from ksefik.ui.permissions import PermissionsScreen

    screen = PermissionsScreen()
    await app.push_screen(screen)
    await settle(pilot)
    screen._grant_type("indirect")
    await settle(pilot)
    form = app.screen
    form.query_one("#f-target_value").focus()
    form.query_one("#row-all_clients").scroll_visible(animate=False)
    await settle(pilot)


@scenario("uprawnienia-przeglad")
async def _grant_review(app, pilot, fake) -> None:
    from ksefik.ui.permissions import PermissionsScreen

    screen = PermissionsScreen()
    await app.push_screen(screen)
    await settle(pilot)
    screen._grant_type("entity")
    await settle(pilot)
    form = app.screen
    values = {"nip": "5260250274", "name": "Biuro Rachunkowe Bilans Sp. z o.o.", "description": "Obsługa faktur"}
    for key, value in values.items():
        form.query_one(f"#f-{key}").value = value
    form.query_one("#f-delegate").value = True
    form.action_submit()
    await settle(pilot)


@scenario("zbiorczy")
async def _collective(app, pilot, fake) -> None:
    from ksefik.ui.account import CollectiveScreen

    screen = CollectiveScreen()
    await app.push_screen(screen)
    await settle(pilot)
    screen.action_new()
    await settle(pilot)
    choices = app.screen.query_one("#f-chosen")
    for index in (0, 2):
        choices.select(choices.get_option_at_index(index).value)
    await settle(pilot)


@scenario("formularz-blad")
async def _form_error(app, pilot, fake) -> None:
    from ksefik.client import KsefError
    from ksefik.ui.permissions import PermissionsScreen

    fake.grant_person_permissions.side_effect = KsefError(
        "Brak uprawnień do wykonania operacji.", http_status=403, details=["Wymagane uprawnienie: CredentialsManage."]
    )
    screen = PermissionsScreen()
    await app.push_screen(screen)
    await settle(pilot)
    screen._grant_type("person")
    await settle(pilot)
    form = app.screen
    for key, value in {"value": "44051401458", "first": "Anna", "last": "Nowak", "description": "Księgowa"}.items():
        form.query_one(f"#f-{key}").value = value
    form.action_submit()
    await settle(pilot)


# ------------------------------------------------------------------------ uruchomienie


async def render(name: str, size: tuple[int, int], out: Path, work: Path) -> Path:
    sys.path.insert(0, str(ROOT / "tests"))
    from ui.harness import build_app

    # Stała, typowa ścieżka profilu na zrzutach (~/.config/ksefik), czyszczona przed każdym ekranem.
    folder = work / ".config" / "ksefik"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    app, fake, store = build_app(folder)
    path = out / f"{name}-{size[0]}x{size[1]}.svg"
    try:
        async with app.run_test(size=size) as pilot:
            await SCENARIOS[name](app, pilot, fake)
            app.save_screenshot(path.name, path=str(out))
    finally:
        store.close()
    return path


def to_png(svg: Path) -> Path:
    """PNG z SVG przez headless Firefox (snap Firefox czyta tylko pliki spod ~/snap/firefox/common)."""
    box = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg.read_text())
    width, height = (math.ceil(float(v)) for v in box.groups())
    png = svg.with_suffix(".png")
    command = ["firefox", "--headless", "--no-remote", "--new-instance", f"--window-size={width},{height}"]
    command += ["--screenshot", str(png), svg.as_uri()]
    subprocess.run(command, check=False, capture_output=True, timeout=120)
    if not png.exists():
        sys.exit(f"Firefox nie utworzył {png} - snap Firefox czyta tylko pliki spod ~/snap/firefox/common.")
    return png


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="ekrany do zrzutu (domyślnie wszystkie)")
    parser.add_argument("--out", type=Path, help="katalog na pliki SVG")
    parser.add_argument("--size", action="append", help="rozmiar SZERxWYS (można powtórzyć)")
    parser.add_argument("--list", action="store_true", help="pokaż dostępne ekrany")
    parser.add_argument("--png", action="store_true", help="zamień SVG na PNG (headless Firefox)")
    args = parser.parse_args()
    if args.list:
        print("\n".join(SCENARIOS))
        return
    if not args.out:
        parser.error("podaj --out")
    unknown = [n for n in args.names if n not in SCENARIOS]
    if unknown:
        parser.error(f"nieznane ekrany: {', '.join(unknown)} (dostępne: {', '.join(SCENARIOS)})")
    sizes = [tuple(int(v) for v in s.split("x")) for s in args.size] if args.size else [(80, 24), (120, 40), (160, 50)]
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        # Aplikacja nie może dotknąć prawdziwego profilu, bazy, tokenów ani katalogu Pobrane.
        os.environ.update(
            HOME=work, XDG_CONFIG_HOME=f"{work}/config", XDG_DATA_HOME=f"{work}/data", KSEF_TOKEN="", TZ="Europe/Warsaw"
        )
        for name in args.names or SCENARIOS:
            for size in sizes:
                path = asyncio.run(render(name, size, args.out.resolve(), Path(work)))
                print(to_png(path) if args.png else path)


if __name__ == "__main__":
    main()

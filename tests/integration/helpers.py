"""Sterowanie aplikacją w testach end-to-end: profil, uruchomienie TUI, oczekiwanie na stan, formularze."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from textual.pilot import Pilot
from textual.widgets import Checkbox, Input, Select, SelectionList, TextArea

from ksefik.client import KsefClient, SendResult
from ksefik.invoice import Draft, Line, Payment, build_xml
from ksefik.profile import BankAccount, Party, load_profile, prepare_data_dir
from ksefik.store import Store
from ksefik.ui.app import KsefApp

log = logging.getLogger("ksef_it")
SIZE = (160, 50)
ALL_SUBJECTS = ("Subject1", "Subject2", "Subject3", "SubjectAuthorized")


def write_profile(
    path: Path, company, partner, *, auth: str = "", sync: str = '["wystawione"]', extra: str = ""
) -> Path:
    """Profil firmy testowej: kontrahenci (krajowy domyślny i z UE), katalog pozycji, dwa rachunki.

    Każdy profil ma własny prefiks numeracji - testy mają osobne bazy lokalne, a KSeF odrzuca duplikaty numerów."""
    path.parent.mkdir(parents=True, exist_ok=True)
    series = secrets.token_hex(2).upper()
    path.write_text(
        f"""\
srodowisko = "test"
synchronizuj_od = "{date.today().isoformat()}"
synchronizuj = {sync}

[logowanie]
{auth or 'metoda = "token"'}

[sprzedawca]
nazwa = "{company.name}"
nip = "{company.nip}"
adres_l1 = "ul. Testowa 1"
adres_l2 = "00-001 Warszawa"
email = "faktury@example.com"

[[rachunki]]
numer = "61109010140000071219812874"
bank = "Bank Testowy"

[[rachunki]]
numer = "PL27114020040000300201355387"
waluta = "EUR"
swift = "BREXPLPW"

[faktura]
numeracja = "IT{series}/{{nr}}/{{mm}}/{{rrrr}}"
numeracja_korekt = "KOR{series}/{{nr}}/{{mm}}/{{rrrr}}"
daty = "dzisiaj"
termin_platnosci_dni = 14
podstawa_zwolnienia = "art. 43 ust. 1 pkt 29 ustawy o VAT"

[[pozycje]]
nazwa = "Usługi programistyczne"
jednostka = "usł."
stawka_vat = "23"
domyslna = true

[[pozycje]]
nazwa = "Software development services"
jednostka = "usł."
stawka_vat = "np II"

[[kontrahenci]]
nazwa = "{partner.name}"
nip = "{partner.nip}"
adres_l1 = "ul. Przykładowa 2"
adres_l2 = "00-002 Warszawa"
domyslny = true

[[kontrahenci]]
nazwa = "Testkunde GmbH"
vat_ue = "DE123456789"
adres_l1 = "Hauptstraße 1"
adres_l2 = "10115 Berlin"
kraj = "DE"
waluta = "EUR"
termin_platnosci_dni = 30
pozycja = "Software development services"
{extra}
""",
        encoding="utf-8",
    )
    return path


def mark_synced(profile_path: Path) -> None:
    """Ustawia „ostatnią synchronizację” na teraz - bez tego aplikacja zsynchronizuje się sama przy starcie
    (a eksporty faktur są limitowane do 20 na godzinę)."""
    profile = load_profile(profile_path)
    store = Store(prepare_data_dir(profile) / "ksef.db")
    for subject in ALL_SUBJECTS:
        store.set_checkpoint(subject, datetime.now(UTC))
    store.close()


@asynccontextmanager
async def running_app(
    profile_path: Path | None, *, sync: bool = False, **kwargs: Any
) -> AsyncIterator[tuple[KsefApp, Pilot]]:
    profile = None
    if profile_path is not None and profile_path.exists():
        profile = load_profile(profile_path)
        if not sync:
            mark_synced(profile_path)
    app = KsefApp(profile, profile_path=profile_path or Path("profile.toml"), **kwargs)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)
        try:
            yield app, pilot
        except BaseException:
            dump_app_state(app)  # aplikacja jeszcze działa - widać, na czym utknął test
            raise


def dump_app_state(app: KsefApp) -> None:
    """Po porażce: stos ekranów, komunikaty i zrzut ekranu (SVG w katalogu logów przebiegu)."""
    try:
        log.info("Ekrany w chwili błędu: %s", " > ".join(type(s).__name__ for s in app.screen_stack))
        for message in notifications(app):
            log.info("Komunikat na ekranie: %s", message.replace("\n", " | "))
        if folder := os.environ.get("KSEF_IT_LOG_DIR"):
            name = f"blad-{time.strftime('%H%M%S')}-{type(app.screen).__name__}.svg"
            app.save_screenshot(name, path=folder)
            log.info("Zrzut ekranu: %s/%s", folder, name)
    except Exception as exc:
        log.warning("Nie udało się zapisać stanu aplikacji: %r", exc)


def notifications(app: KsefApp) -> list[str]:
    return [n.message for n in app._notifications]


class AppError(AssertionError):
    """Aplikacja pokazała komunikat o błędzie."""


async def wait_until(pilot: Pilot, predicate: Callable[[], Any], what: str, timeout: float = 90) -> Any:
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + timeout
    log.debug("Czekam na: %s (do %ss)", what, timeout)
    while True:
        try:
            result = predicate()
        except AppError:
            raise
        except Exception:
            result = None
        if result:
            waited = loop.time() - started
            (log.info if waited > 10 else log.debug)("Doczekano: %s po %.1fs", what, waited)
            return result
        if loop.time() > deadline:
            log.error("Nie doczekano się: %s po %.0fs (ekran %s)", what, timeout, type(pilot.app.screen).__name__)
            app = pilot.app
            raise AssertionError(
                f"Nie doczekano się: {what} (ekran: {type(app.screen).__name__}, komunikaty: {notifications(app)})"
            )
        await pilot.pause(0.25)


async def wait_screen(pilot: Pilot, cls: type, timeout: float = 60) -> Any:
    return await wait_until(
        pilot, lambda: pilot.app.screen if isinstance(pilot.app.screen, cls) else None, cls.__name__, timeout
    )


async def wait_notification(pilot: Pilot, text: str, timeout: float = 120) -> str:
    """Czeka na komunikat zawierający `text`; komunikat o błędzie przerywa oczekiwanie od razu."""

    def found() -> str | None:
        for notification in pilot.app._notifications:
            if text in notification.message:
                return notification.message
            if notification.severity == "error":
                raise AppError(f"Aplikacja zgłosiła błąd zamiast „{text}”: {notification.message}")
        return None

    return await wait_until(pilot, found, f"komunikat „{text}”", timeout)


async def wait_items(pilot: Pilot, screen: Any, timeout: float = 90, minimum: int = 1) -> list[dict]:
    """Czeka, aż ekran listy (ListScreen) wczyta co najmniej `minimum` pozycji."""

    def loaded() -> list[dict] | None:
        return screen.items if len(screen.items) >= minimum else None

    return await wait_until(pilot, loaded, f"{type(screen).__name__}: co najmniej {minimum} pozycji", timeout)


async def wait_loaded(pilot: Pilot, screen: Any, timeout: float = 90) -> None:
    """Czeka na zakończenie wczytywania listy (dane albo komunikat), a błąd zamienia na porażkę testu."""

    def done() -> bool:
        empty = screen.query_one("#list-empty")
        text = str(empty.render()) if empty.display else ""
        return bool(screen.items) or (text and "Wczytuję" not in text)

    await wait_until(pilot, done, f"{type(screen).__name__}: wczytanie danych", timeout)
    empty = screen.query_one("#list-empty")
    if not screen.items and empty.display:
        message = str(empty.render())
        assert message == screen.EMPTY, f"{type(screen).__name__}: {message}"


def set_value(screen: Any, selector: str, value: Any) -> None:
    widget = screen.query_one(selector)
    if isinstance(widget, Input):
        widget.value = str(value)
    elif isinstance(widget, Select | Checkbox):
        widget.value = value
    elif isinstance(widget, SelectionList):
        widget.deselect_all()
        for item in value:
            widget.select(item)
    elif isinstance(widget, TextArea):
        widget.text = str(value)
    else:
        raise TypeError(f"{selector}: nieobsługiwany widżet {type(widget).__name__}")


async def fill_form(pilot: Pilot, values: dict[str, Any], *, submit: bool = True) -> None:
    """Wypełnia FormScreen (pola #f-<klucz>) i zatwierdza go przyciskiem."""
    from textual.widgets import Button

    from ksefik.ui.common import ConfirmScreen, FormScreen

    screen = await wait_screen(pilot, FormScreen)
    for key, value in values.items():
        set_value(screen, f"#f-{key}", value)
        await pilot.pause(0.05)
    if submit:
        screen.query_one("#submit", Button).press()
        await pilot.pause(0.3)
        if screen.review is not None and isinstance(pilot.app.screen, ConfirmScreen):
            pilot.app.screen.query_one("#confirm", Button).press()  # przegląd przed nadaniem uprawnień
            await pilot.pause(0.3)
        # Formularz z operacją w KSeF czeka otwarty na odpowiedź i zamyka się dopiero po sukcesie.
        await wait_until(pilot, lambda: not screen.busy, "odpowiedź KSeF na formularz")
        await pilot.pause(0.1)
        assert not isinstance(pilot.app.screen, FormScreen) or screen is not pilot.app.screen, (
            f"formularz nie został przyjęty: {screen.query_one('#form-error').render()}"
        )


async def choose(pilot: Pilot, label: str) -> None:
    """Wybiera opcję o podanej etykiecie w ChoiceScreen albo w liście OptionList na ekranie."""
    from textual.widgets import OptionList

    option_list = await wait_until(pilot, lambda: pilot.app.screen.query_one(OptionList), "lista opcji")
    for index in range(option_list.option_count):
        option = option_list.get_option_at_index(index)
        if label in str(option.prompt):
            option_list.highlighted = index
            option_list.action_select()
            await pilot.pause(0.3)
            return
    raise AssertionError(f"Brak opcji „{label}”")


# ------------------------------------------------------------------- dane przez API


def draft_for(seller, buyer_nip: str, number: str, *, amount: str = "100.00", third=None, **changes: Any) -> Draft:
    """Prosta faktura wystawiona przez `seller` (Company) - do przygotowania danych testu przez API."""
    from ksefik.profile import ThirdParty

    today = date.today()
    draft = Draft(
        number=number,
        issue_date=today,
        sale_date=today,
        seller=Party(nazwa=seller.name, nip=seller.nip, adres_l1="ul. Kontrahenta 5", adres_l2="00-005 Warszawa"),
        buyer=Party(nazwa=f"Nabywca {buyer_nip}", nip=buyer_nip, adres_l1="ul. Nabywcy 7", adres_l2="00-007 Warszawa"),
        lines=(Line("Usługa testowa", Decimal(1), "usł.", Decimal(amount), "23"),),
        payment=Payment(
            form="6", due_date=today + timedelta(days=7), accounts=(BankAccount("61109010140000071219812874"),)
        ),
    )
    if third is not None:
        party = Party(nazwa=third.name, nip=third.nip, adres_l1="ul. Testowa 1", adres_l2="00-001 Warszawa")
        draft = replace(draft, third_parties=(ThirdParty(party, rola=6),))
    return replace(draft, **changes)


async def send_drafts(client: KsefClient, drafts: list[Draft]) -> list[tuple[Draft, bytes, SendResult]]:
    results = []
    for draft in drafts:
        xml = build_xml(draft)
        results.append((draft, xml, await client.send_invoice(xml)))
    return results


async def wait_stored(client: KsefClient, results: list[tuple[Draft, bytes, SendResult]], timeout: float = 600) -> None:
    """Czeka, aż KSeF trwale zapisze faktury - dopiero wtedy obejmuje je eksport (synchronizacja)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    pending = list(results)
    while pending:
        still = []
        for item in pending:
            status = await client.invoice_status(item[2].session_ref, item[2].invoice_ref)
            if not status.get("permanentStorageDate"):
                still.append(item)
        pending = still
        if pending:
            if loop.time() > deadline:
                raise AssertionError(f"KSeF nie zapisał trwale faktur: {[d.number for d, _, _ in pending]}")
            await asyncio.sleep(5)

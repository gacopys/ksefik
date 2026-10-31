"""Pierwsze uruchomienie: kreator profilu firmy, token KSeF albo hasło do klucza certyfikatu."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Input, Label, Static

from .. import registry
from ..client import KsefClient, KsefError, TokenCredentials
from ..config import ENVIRONMENTS
from ..invoice import format_number
from ..money import fmt_account
from ..profile import BankAccount, InvoiceSettings, Party, Profile, ProfileError, load_profile, save_new_profile
from ..tokenstore import env_token_set, save_token, storage_label
from ..xades import CredentialsError, PasswordRequired, Signer, load_signer
from .common import FieldAction, Filled, FormField, FormScreen, check_nip, short_path

if TYPE_CHECKING:
    from .app import KsefApp

NUMBERING_PRESETS = ["FV/{nr}/{mm}/{rrrr}", "{nr}/{mm}/{rrrr}", "{nr}/{m}/{rrrr}", "FV/{rrrr}/{nr:4}", "{nr}/{rrrr}"]


WIZARD_STEPS = 2


async def lookup_seller(nip: str) -> Filled:
    """Nazwa i adres firmy z wykazu podatników VAT; jedyny rachunek z wykazu przechodzi do kroku 2."""
    try:
        company = await registry.find_company(nip)
    except registry.RegistryError as exc:
        return Filled({}, error=str(exc))
    values: dict[str, Any] = {"name": company.name, "l1": company.address_l1, "l2": company.address_l2}
    notes = ["[$text-success]✓ Uzupełniono z wykazu podatników VAT - sprawdź dane.[/]"]
    if len(company.accounts) == 1:
        values["account"] = fmt_account(company.accounts[0])
        notes.append("[$foreground-muted]Rachunek z wykazu wpiszę w kroku 2.[/]")
    elif company.accounts:
        count = len(company.accounts)
        notes.append(f"[$foreground-muted]Rachunków w wykazie: {count} - właściwy wpisz w kroku 2.[/]")
    if company.vat_note:
        notes.append(f"[$text-warning]{escape(company.vat_note)}[/]")
    return Filled(values, note="\n".join(notes))


def profile_wizard(
    path: Path, values: dict[str, Any] | None = None, *, step: int = 1, env: str | None = None
) -> FormScreen:
    """Kreator profilu: krok 1 - firma i środowisko, krok 2 - ustawienia faktur. `values` - już wpisane dane."""
    values = values or {}
    today = date.today()

    def value(key: str, default: Any = "") -> Any:
        return values.get(key) if values.get(key) is not None else default

    if step == 1:
        env_note = f"\n[$text-warning]Uruchomiono z --env {escape(env)}.[/]" if env else ""
        return FormScreen(
            f"Witaj w ksefik · krok 1 z {WIZARD_STEPS}: firma",
            [
                FormField(
                    "env",
                    "Środowisko KSeF",
                    "select",
                    value("env", env or "prod"),
                    [(v["label"], k) for k, v in ENVIRONMENTS.items()],
                ),
                FormField(
                    "nip",
                    "NIP firmy",
                    value=value("nip"),
                    required=True,
                    check=check_nip,
                    restrict=r"[0-9]*",
                    max_length=10,
                    hint="Nazwa i adres z wykazu podatników VAT (Biała lista MF).",
                    action=FieldAction("Pobierz dane", lookup_seller),
                ),
                FormField("name", "Nazwa firmy (jak w CEIDG/KRS)", value=value("name"), required=True),
                FormField(
                    "l1", "Ulica i numer", value=value("l1"), required=True, placeholder="ul. Przykładowa 1 lok. 2"
                ),
                FormField("l2", "Kod pocztowy i miejscowość", value=value("l2"), placeholder="00-001 Warszawa"),
            ],
            intro=(
                "Podaj dane swojej firmy - trafią na wystawiane faktury." + env_note + "\n"
                f"[$foreground-muted]Zapiszę je w {escape(short_path(path))}.[/]"
            ),
            submit="Dalej →",
            wide=True,
            local=True,
        )
    numbering = [(f"{format_number(p, 1, today)}   ({p})", p) for p in NUMBERING_PRESETS]
    return FormScreen(
        f"Witaj w ksefik · krok 2 z {WIZARD_STEPS}: faktury",
        [
            FormField(
                "account",
                "Rachunek bankowy (opcjonalnie)",
                value=value("account"),
                placeholder="61 1090 1014 0000 0712 1981 2874",
                check=lambda v: None if 10 <= len(re.sub(r"\s", "", v)) <= 34 else "od 10 do 34 znaków",
            ),
            FormField(
                "numbering",
                "Numeracja faktur",
                "select",
                value("numbering", NUMBERING_PRESETS[0]),
                numbering,
                required=True,
            ),
            FormField(
                "dates",
                "Daty nowej faktury",
                "select",
                value("dates", "dzisiaj"),
                [
                    ("Wystawienie i sprzedaż: dzisiaj", "dzisiaj"),
                    ("Wystawienie dzisiaj, usługa: koniec miesiąca", "dzisiaj_za_miesiac"),
                    ("Obie daty: ostatni dzień miesiąca", "koniec_miesiaca"),
                ],
                required=True,
            ),
            FormField(
                "days",
                "Termin płatności (dni)",
                value=value("days", "14"),
                restrict=r"[0-9]*",
                max_length=3,
                required=True,
            ),
        ],
        intro=(
            "Domyślne ustawienia nowych faktur - zmienisz je w każdej fakturze.\n"
            "[$foreground-muted]Kontrahentów, katalog pozycji, kolejne rachunki i pozostałe ustawienia dodasz później "
            "w profilu (Ctrl+P → „Edytuj profil”).[/]"
        ),
        submit="Zapisz profil",
        wide=True,
        local=True,
        back="← Wstecz",
    )


def profile_from_wizard(path: Path, values: dict[str, Any]) -> Profile:
    """Zapisuje profile.toml z danych kreatora i wczytuje go ponownie (z pełną walidacją)."""
    account = re.sub(r"\s", "", values.get("account") or "")
    profile = Profile(
        path=path,
        env=values["env"] or "prod",
        seller=Party(nazwa=values["name"], nip=values["nip"], adres_l1=values["l1"], adres_l2=values.get("l2") or ""),
        accounts=(BankAccount(account),) if account else (),
        invoice=InvoiceSettings(
            numeracja=values["numbering"],
            daty=values["dates"],
            dzien_graniczny=10 if values["dates"] != "dzisiaj" else 0,
            termin_platnosci_dni=int(values["days"] or 14),
        ),
    )
    save_new_profile(profile)
    try:
        return load_profile(path)
    except ProfileError:
        path.unlink(missing_ok=True)
        raise


class TokenScreen(Screen[KsefClient | None]):
    """Token KSeF: sprawdzany w KSeF przed zapisem. Zwraca klienta albo None (praca lokalna / anulowanie)."""

    BINDINGS = [Binding("escape", "cancel", "Pracuj lokalnie")]

    app: KsefApp

    def __init__(self, first_run: bool = True) -> None:
        super().__init__()
        self.first_run = first_run

    def compose(self) -> ComposeResult:
        profile = self.app.profile
        storage = storage_label(profile.env, profile.context_id).replace(str(Path.home()), "~")
        with VerticalScroll(id="setup-box", classes="dialog"):
            yield Static("[b]KSeF[/b] · faktury w terminalu", classes="dialog-title")
            if env_token_set():
                yield Static(
                    "[$text-warning]Ustawiona jest zmienna KSEF_TOKEN - przy kolejnych uruchomieniach "
                    "aplikacja użyje tokena ze zmiennej, a nie zapisanego tutaj.[/]",
                    classes="dialog-text",
                )
            yield Static(
                f"Firma: [b]{escape(profile.seller.nazwa)}[/b]  [$foreground-muted]NIP {profile.seller.nip}[/]\n"
                f"Środowisko: [b]{profile.env_label}[/b]\n\n"
                "Aby połączyć się z KSeF, wklej [b]token KSeF[/b] z uprawnieniami do "
                "przeglądania i wystawiania faktur. Możesz też pracować lokalnie - na fakturach już "
                "pobranych i szkicach - i połączyć się później.\n"
                "[$foreground-muted]Token wygenerujesz w Aplikacji Podatnika KSeF (zakładka z tokenami). "
                f"Zapiszę go {escape(storage)}. Logowanie certyfikatem włączysz w profilu: "
                + escape('[logowanie] metoda = "certyfikat"')
                + ".[/]",
                classes="dialog-text",
            )
            yield Label("Token KSeF", classes="field-label")
            with Horizontal(classes="token-row"):
                yield Input(placeholder="wklej token (Ctrl+V / Ctrl+Shift+V)", password=True, id="token")
                yield Checkbox("pokaż", id="show-token", compact=True)
            yield Static("", id="setup-status")
            with Horizontal(classes="dialog-buttons dialog-footer"):
                if self.first_run:
                    yield Button("Wyjdź", id="quit")
                    yield Button("Pracuj lokalnie", id="cancel")
                else:
                    yield Button("Anuluj", id="cancel")
                yield Button("Połącz", variant="primary", id="connect")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#token", Input).focus()

    @on(Checkbox.Changed, "#show-token")
    def _toggle(self, event: Checkbox.Changed) -> None:
        self.query_one("#token", Input).password = not event.value

    @on(Input.Submitted, "#token")
    @on(Button.Pressed, "#connect")
    def _connect_pressed(self) -> None:
        token = self.query_one("#token", Input).value.strip()
        if not token:
            self.query_one("#setup-status", Static).update("[$text-warning]Najpierw wklej token.[/]")
            return
        self.connect(token)

    @work(exclusive=True)
    async def connect(self, token: str) -> None:
        status = self.query_one("#setup-status", Static)
        button = self.query_one("#connect", Button)
        button.disabled = True
        status.update("[$text-accent]Łączę się z KSeF…[/]")
        profile = self.app.profile
        client = self.app.make_client(TokenCredentials(token))
        try:
            await client.ensure_auth()
        except KsefError as exc:
            await client.close()
            status.update(
                f"[$text-error]{escape(exc.human())}[/]\n[$foreground-muted]Sprawdź token i spróbuj ponownie.[/]"
            )
            button.disabled = False
            return
        where = save_token(profile.env, profile.context_id, token)
        self.notify(f"Połączono z KSeF. Token zapisano: {where}.", title="Gotowe", timeout=5)
        self.dismiss(client)

    @on(Button.Pressed, "#quit")
    def _quit(self) -> None:
        self.app.exit()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class PasswordScreen(Screen[Signer | None]):
    """Hasło do zaszyfrowanego klucza prywatnego certyfikatu (nie jest nigdzie zapisywane)."""

    BINDINGS = [Binding("escape", "cancel", "Pracuj lokalnie")]

    app: KsefApp

    def compose(self) -> ComposeResult:
        auth = self.app.profile.auth
        files = escape(short_path(auth.certificate_path)) if auth.certificate_path else ""
        if auth.key_path:
            files += f"\n[$foreground-muted]klucz:[/] {escape(short_path(auth.key_path))}"
        with VerticalScroll(id="setup-box", classes="dialog"):
            yield Static("[b]Logowanie certyfikatem[/b]", classes="dialog-title")
            yield Static(
                f"Klucz prywatny jest chroniony hasłem.\n[$foreground-muted]certyfikat:[/] {files}\n\n"
                "[$foreground-muted]Hasło nie jest zapisywane. Możesz je też podać w zmiennej KSEF_KEY_PASSWORD.[/]",
                classes="dialog-text",
            )
            yield Label("Hasło do klucza", classes="field-label")
            yield Input(password=True, id="password")
            yield Static("", id="setup-status")
            with Horizontal(classes="dialog-buttons dialog-footer"):
                yield Button("Pracuj lokalnie", id="cancel")
                yield Button("Odblokuj", variant="primary", id="unlock")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#password", Input).focus()

    @on(Input.Submitted, "#password")
    @on(Button.Pressed, "#unlock")
    def _unlock(self) -> None:
        auth = self.app.profile.auth
        password = self.query_one("#password", Input).value
        try:
            signer = load_signer(auth.certificate_path, auth.key_path, password or None)
        except PasswordRequired:
            self.query_one("#setup-status", Static).update("[$text-warning]Podaj hasło.[/]")
            return
        except CredentialsError as exc:
            self.query_one("#setup-status", Static).update(f"[$text-error]{escape(str(exc))}[/]")
            return
        self.dismiss(signer)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)

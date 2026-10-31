"""Aplikacja Textual: profil, baza, połączenie z KSeF i nawigacja między ekranami."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from textual import events, work
from textual.app import App, SuspendNotSupported, SystemCommand
from textual.binding import Binding
from textual.command import CommandPalette, Hit, Hits
from textual.markup import escape
from textual.reactive import reactive
from textual.screen import Screen
from textual.system_commands import SystemCommandsProvider
from textual.theme import Theme, ThemeProvider

from ..client import (
    AuthError,
    CertificateCredentials,
    Credentials,
    KsefClient,
    KsefError,
    RateLimitError,
    TokenCredentials,
)
from ..config import os_error_text
from ..drafts import dump, load_draft
from ..invoice import Draft, new_draft
from ..profile import Profile, ProfileError, load_profile, prepare_data_dir, set_top_level
from ..store import Store
from ..submissions import reconcile
from ..tokenstore import delete_token, env_token_set, load_token, storage_label
from ..xades import CredentialsError, PasswordRequired, Signer, load_signer
from .common import (
    BACK,
    SELECT_HINT,
    ChoiceScreen,
    ConfirmScreen,
    FormField,
    FormScreen,
    HelpScreen,
    Saved,
    TooSmallScreen,
    ValueScreen,
    can_open_browser,
    check_folder,
    downloads_dir,
    draft_saved_text,
    save_download,
    short_path,
    system_clipboard,
    too_small,
)
from .setup_screen import WIZARD_STEPS, PasswordScreen, TokenScreen, profile_from_wizard, profile_wizard

# Słowa, po których paleta znajduje polecenie, choć nie ma ich w jego nazwie.
SYNONYMS = {
    "Wysyłki i UPO": ("potwierdzenie odbioru", "status wysyłki", "wysłane faktury"),
    "Sesje w KSeF": ("upo sesji", "wysyłki z innych programów"),
    "Edytuj profil": ("ustawienia", "konfiguracja", "dane firmy", "numeracja"),
    "Folder zapisu plików": ("pobrane", "eksport", "ustawienia"),
    "Wyloguj z KSeF": ("rozłącz", "praca lokalna", "zapomnij token"),
    "Motyw": ("kolory", "wygląd", "ciemny", "jasny"),
    "Zakończ": ("wyjście", "zamknij"),
    "Pomoc i skróty klawiszowe": ("klawisze", "skróty"),
}


class KsefCommands(SystemCommandsProvider):
    """Polecenia aplikacji w palecie - znajdowane także po synonimach (np. „potwierdzenie” → Wysyłki i UPO)."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, callback, *_ in self.app.get_system_commands(self.screen):
            score = matcher.match(name)
            if not score:
                score = max((matcher.match(word) for word in SYNONYMS.get(name, ())), default=0) * 0.9
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_text)


class KsefApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "KSeF"
    ENABLE_COMMAND_PALETTE = True
    COMMANDS = {KsefCommands}
    BINDINGS = [
        Binding("ctrl+p", "command_palette", "Polecenia", show=False, priority=True),
        Binding("f1", "help", "Pomoc"),
    ]

    # Stan połączenia pokazywany w pasku u góry: "online" (są dane logowania), "local" (praca bez KSeF
    # z wyboru użytkownika) albo "login" (potrzebne logowanie - brak albo odrzucone dane).
    connection: reactive[str] = reactive("login")

    def __init__(
        self,
        profile: Profile | None,
        *,
        profile_path: Path,
        env_override: str | None = None,
        notice: str = "",
        store: Store | None = None,
    ) -> None:
        super().__init__()
        self.profile_path = profile_path
        self.env_override = env_override
        self.notice = notice
        self._profile = profile
        self._store = store
        self._client: KsefClient | None = None
        # Wysyłki trwające w tym uruchomieniu (id z historii) - sprawdzanie zaległych wysyłek ich nie rusza.
        self.active_sends: set[int] = set()
        self._login: asyncio.Future[KsefClient | None] | None = None  # otwarte okno logowania
        self.clipboard_confirmed = False  # czy ostatnie kopiowanie przyjął schowek systemowy
        self.sync_blocked_until: datetime | None = None  # przerwa w zapytaniach, o którą prosi KSeF (Retry-After)

    def notify(self, message: str, *, title: str = "", severity="information", timeout=None, markup=True) -> None:
        # Komunikaty zawierają teksty z KSeF i ścieżki (np. „[logowanie]”) - nigdy nie interpretujemy ich jako markup.
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=False)

    @property
    def client(self) -> KsefClient | None:
        return self._client

    @client.setter
    def client(self, value: KsefClient | None) -> None:
        self._client = value
        if value is not None:
            self.connection = "online"

    def work_locally(self) -> None:
        """Praca bez połączenia z KSeF: lista, wyszukiwanie, podgląd i szkice działają na lokalnej bazie."""
        self._client = None
        self.connection = "local"

    def login_failed(self) -> None:
        """KSeF odrzucił dane logowania (np. unieważniony token) - przy następnej operacji zapytamy o nowe."""
        self._disconnect("login")

    @property
    def profile(self) -> Profile:
        assert self._profile is not None, "profil nie został jeszcze wczytany"
        return self._profile

    @property
    def store(self) -> Store:
        assert self._store is not None, "baza nie została jeszcze otwarta"
        return self._store

    def on_resize(self, event: events.Resize) -> None:
        self._check_size(event.size)

    def push_screen(self, screen: Any, *args: Any, **kwargs: Any) -> Any:
        result = super().push_screen(screen, *args, **kwargs)
        if not isinstance(screen, TooSmallScreen):
            self.call_after_refresh(self._check_size)  # nowy ekran nie może przykryć zasłony
        return result

    def _check_size(self, size: tuple[int, int] | None = None) -> None:
        """Poniżej 80×24 interfejs byłby nieczytelny - zasłaniamy go komunikatem, niczego nie zamykając."""
        small = too_small(size or self.size)
        top = self.screen_stack[-1] if self.screen_stack else None
        if small and not isinstance(top, TooSmallScreen):
            self.push_screen(TooSmallScreen())
        elif isinstance(top, TooSmallScreen):
            if small:
                top.update_size(size)
            else:
                self.pop_screen()

    def on_mount(self) -> None:
        self.call_after_refresh(self._check_size)
        self._use_profile_theme()
        if self._profile is None:
            self._run_wizard()
        else:
            self._profile_ready()

    def _use_profile_theme(self) -> None:
        self.theme = self._profile.theme if self._profile else "textual-dark"
        self.theme_changed_signal.subscribe(self, self._remember_theme)

    def _remember_theme(self, theme: Theme) -> None:
        """Motyw wybrany w palecie poleceń zapisujemy w profilu, żeby został po ponownym uruchomieniu."""
        profile = self._profile
        if profile is None or theme.name == profile.theme:
            return
        try:
            set_top_level(profile.path, "motyw", theme.name)
        except OSError as exc:
            self.notify(f"Nie udało się zapisać motywu w profilu: {os_error_text(exc)}", severity="warning")
            return
        self._profile = replace(profile, theme=theme.name)

    def _run_wizard(self, values: dict[str, Any] | None = None, step: int = 1) -> None:
        """Kreator profilu krok po kroku; wpisane dane przechodzą między krokami i przetrwają błąd zapisu."""
        values = dict(values or {})

        def answered(result: dict[str, Any] | None) -> None:
            if result is None:
                self.exit(message="Profil nie został utworzony.")
                return
            merged = {**values, **{k: v for k, v in result.items() if k != BACK}}
            if result.get(BACK):
                self._run_wizard(merged, step - 1)
            elif step < WIZARD_STEPS:
                self._run_wizard(merged, step + 1)
            else:
                self._wizard_done(merged)

        self.push_screen(profile_wizard(self.profile_path, values, step=step, env=self.env_override), answered)

    def _wizard_done(self, values: dict) -> None:
        try:
            self._profile = profile_from_wizard(self.profile_path, values).with_env(self.env_override)
        except ProfileError as exc:
            self.notify(str(exc), title="Nie udało się zapisać profilu", severity="error", timeout=15)
            self._run_wizard(values)  # wpisane dane zostają - wystarczy poprawić
            return
        self.notice = f"Profil zapisany: {short_path(self.profile_path)}"
        self._profile_ready()

    def _profile_ready(self) -> None:
        if self._store is None:
            self._store = Store(prepare_data_dir(self.profile) / "ksef.db")
        if self.notice:
            self.notify(self.notice, title="Profil", timeout=8)
        auth = self.profile.auth
        if auth.metoda == "token":
            if token := load_token(self.profile.env, self.profile.context_id):
                self.client = self.make_client(TokenCredentials(token))
                self._start()
            else:
                self.push_screen(TokenScreen(first_run=True), self._token_entered)
            return
        signer = self._load_signer(os.environ.get("KSEF_KEY_PASSWORD"))
        if isinstance(signer, Signer):
            self.client = self.make_client(self._certificate_credentials(signer))
            self._start()
        elif signer == "password":
            self.push_screen(PasswordScreen(), self._password_entered)
        else:
            self._start()  # błędny certyfikat w profilu - komunikat już jest, logowanie nie ruszy

    def _load_signer(self, password: str | None) -> Signer | str:
        auth = self.profile.auth
        try:
            return load_signer(auth.certificate_path, auth.key_path, password)
        except PasswordRequired:
            return "password"
        except CredentialsError as exc:
            self.notify(
                f"{exc}\nPopraw sekcję [logowanie] w profilu.", title="Certyfikat", severity="error", timeout=15
            )
            return "error"

    def _certificate_credentials(self, signer: Signer) -> CertificateCredentials:
        subject = "certificateFingerprint" if self.profile.auth.identyfikacja == "odcisk" else "certificateSubject"
        return CertificateCredentials(signer, subject)

    def _token_entered(self, client: KsefClient | None) -> None:
        if client is not None:
            self.client = client
        else:
            self.work_locally()
        self._start()

    def _password_entered(self, signer: Signer | None) -> None:
        if signer is not None:
            self.client = self.make_client(self._certificate_credentials(signer))
        else:
            self.work_locally()
        self._start()

    def _start(self) -> None:
        from .main_screen import MainScreen

        self.push_screen(MainScreen())
        if self.store.unresolved_sent():
            self.reconcile_all()

    def make_client(self, credentials: Credentials) -> KsefClient:
        profile = self.profile
        client = KsefClient(
            profile.api_url,
            profile.context_id,
            credentials,
            context_type=profile.context_type,
            allowed_ips=profile.auth.dozwolone_ip,
            verify_certificate_chain=profile.auth.weryfikuj_certyfikat,
        )
        client.on_credentials_rejected = lambda: self._rejected(client)
        return client

    def _rejected(self, client: KsefClient) -> None:
        if client is self._client:
            self.login_failed()

    async def action_quit(self) -> None:
        """Wyjście: zapisuje otwarte szkice; przy trwającej wysyłce pyta, a o zaległych wysyłkach przypomina."""
        from .editor import EditScreen
        from .import_screen import ImportScreen
        from .invoice_screens import SendScreen

        for screen in self.screen_stack:
            if isinstance(screen, EditScreen):
                screen.flush()
        busy = any(
            (isinstance(screen, SendScreen) and not screen.finished)
            or (isinstance(screen, ImportScreen) and screen.sending)
            for screen in self.screen_stack
        )
        if not busy:
            self._quit_now()
            return

        def confirmed(ok: bool | None) -> None:
            if ok:
                self._quit_now()

        self.push_screen(
            ConfirmScreen(
                "Trwa wysyłka do KSeF",
                "Jeśli zamkniesz aplikację teraz, wynik wysyłki sprawdzimy przy następnym uruchomieniu. "
                "Faktury nie trzeba wysyłać ponownie.",
                confirm="Zamknij mimo to",
            ),
            confirmed,
        )

    def _quit_now(self) -> None:
        pending = len(self.store.unresolved_sent()) if self._store else 0
        message = None
        if pending:
            message = (
                f"Wysyłki czekające na wynik z KSeF: {pending}. "
                "Wysyłka zostanie sprawdzona przy następnym uruchomieniu."
            )
        self.exit(message=message)

    async def on_unmount(self) -> None:
        if self.client:
            await self.client.close()
        if self._store:
            self._store.close()

    # ------------------------------------------------------------- usługi

    async def get_client(self) -> KsefClient | None:
        """Klient KSeF; gdy brak danych logowania, pyta o nie. None - użytkownik zrezygnował.

        Kilka operacji naraz czeka na to samo okno logowania (nigdy dwa okna jedno na drugim), a przerwanie
        jednej z nich nie zostawia osieroconego okna.
        """
        if self.client is not None:
            return self.client
        if self._login is None or self._login.done():
            self._login = self._ask_for_login()
        return await asyncio.shield(self._login)

    def _ask_for_login(self) -> asyncio.Future[KsefClient | None]:
        future: asyncio.Future[KsefClient | None] = asyncio.get_running_loop().create_future()

        def finish(result: KsefClient | Signer | None) -> None:
            if isinstance(result, Signer):
                self.client = self.make_client(self._certificate_credentials(result))
            elif result is not None:
                self.client = result
            if not future.done():
                future.set_result(self.client)

        if self.profile.auth.metoda == "token":
            # Po „Pracuj lokalnie” zapisany token wciąż jest dobry - łączymy się nim bez pytania.
            # Po odrzuceniu danych logowania (stan "login") trzeba podać nowy token.
            token = load_token(self.profile.env, self.profile.context_id) if self.connection == "local" else None
            if token:
                finish(self.make_client(TokenCredentials(token)))
            else:
                self.push_screen(TokenScreen(first_run=False), finish)
            return future
        signer = self._load_signer(os.environ.get("KSEF_KEY_PASSWORD"))
        if signer == "password":
            self.push_screen(PasswordScreen(), finish)
        else:
            finish(signer if isinstance(signer, Signer) else None)
        return future

    # ------------------------------------------------- schowek, przeglądarka, pliki

    def copy_to_clipboard(self, text: str) -> None:
        """Schowek terminala (OSC 52, działa też przez SSH) i systemowy - np. GNOME Terminal nie obsługuje OSC 52."""
        super().copy_to_clipboard(text)
        self.clipboard_confirmed = system_clipboard(text)

    def copy_text(self, text: str, what: str) -> None:
        """Kopiuje z komunikatem; gdy nie da się potwierdzić schowka, pokazuje tekst do zaznaczenia."""
        self.copy_to_clipboard(text)
        if self.clipboard_confirmed:
            self.notify(f"Skopiowano {what}:\n{text}", timeout=4)
            return
        self.push_screen(
            ValueScreen(
                f"Skopiuj {what}",
                "Tekst wysłano do schowka terminala, ale nie da się sprawdzić, czy dotarł (np. przez SSH). "
                f"Jeśli wklejenie nie zadziała, {SELECT_HINT}.",
                text,
            )
        )

    def action_open_link(self, url: str) -> None:
        if not can_open_browser():
            self.push_screen(
                ValueScreen(
                    "Otwórz link w przeglądarce",
                    "W tej sesji nie ma przeglądarki (np. połączenie SSH). "
                    "Skopiuj link i otwórz go na swoim komputerze.",
                    url,
                    copy="Kopiuj link",
                )
            )
            return
        self.open_url(url)
        self.notify(f"Otwieram w przeglądarce:\n{url}\nJeśli się nie otworzyła, skopiuj link.", timeout=6)

    @property
    def export_dir(self) -> Path:
        """Folder zapisu plików: wybrany przez użytkownika (zapamiętany w bazie tej firmy) albo Pobrane."""
        chosen = self._store.setting("export_dir") if self._store else None
        if chosen and Path(chosen).is_dir():
            return Path(chosen)
        return downloads_dir()

    def export_file(self, name: str, data: bytes, *, title: str) -> None:
        self.export_files([(name, data)], title=title)

    def export_files(self, files: list[tuple[str, bytes]], *, title: str, folder: Path | None = None) -> None:
        """Zapisuje pliki i mówi gdzie; gdy folder nie przyjmuje plików, proponuje wybór innego."""
        folder = folder or self.export_dir
        saved: list[Saved] = []
        for name, data in files:
            result = save_download(name, data, folder)
            if not result.ok:
                self._export_failed(files, title, folder, result.error)
                return
            saved.append(result)
        if len(saved) == 1:
            text = "Ten plik jest już zapisany:" if saved[0].existing else "Zapisano plik:"
            self.notify(f"{text}\n{short_path(saved[0].path)}", title=title, timeout=6)
        else:
            names = "\n".join(s.path.name + (" (już był)" if s.existing else "") for s in saved)
            self.notify(f"Zapisano w {short_path(folder)}:\n{names}", title=title, timeout=8)

    def _export_failed(self, files: list[tuple[str, bytes]], title: str, folder: Path, error: str) -> None:
        def chosen(new_folder: Path | None) -> None:
            if new_folder is not None:
                self.export_files(files, title=title, folder=new_folder)

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.choose_export_dir(chosen)

        self.push_screen(
            ConfirmScreen(
                title,
                f"Nie udało się zapisać pliku w {escape(short_path(folder))}:\n[b]{escape(error)}[/b]\n\n"
                "Wybierz inny folder - zapamiętamy go na kolejne zapisy.",
                confirm="Wybierz folder",
                danger=False,
                focus_cancel=False,
            ),
            confirmed,
        )

    def choose_export_dir(self, then: Callable[[Path | None], None] | None = None) -> None:
        """Wybór folderu zapisu plików (XML, UPO) - zapamiętywany w bazie tej firmy, nie w profilu."""

        def entered(values: dict[str, Any] | None) -> None:
            folder = Path(values["folder"]).expanduser().resolve() if values else None
            if folder is not None:
                self.store.set_setting("export_dir", str(folder))
                if then is None:
                    self.notify(f"Pliki będą zapisywane w {short_path(folder)}.", timeout=5)
            if then is not None:
                then(folder)

        self.push_screen(
            FormScreen(
                "Folder zapisu plików",
                [
                    FormField(
                        "folder",
                        "Folder",
                        value=short_path(self.export_dir),
                        required=True,
                        check=check_folder,
                        hint="Tu trafiają zapisywane XML i UPO. Np. ~/Dokumenty/faktury",
                    )
                ],
                submit="Zapisuj tutaj",
                submit_on_enter=True,
                local=True,
            ),
            entered,
        )

    def open_invoice(self, ksef_number: str, direction: str) -> None:
        from .invoice_screens import InvoiceScreen

        if row := self.store.get(ksef_number, direction):
            self.push_screen(InvoiceScreen(row))

    def numbers_in_use(self, exclude_draft: int | None = None) -> list[str]:
        """Numery, których nie wolno nadać nowej fakturze: wystawione, wysłane i w otwartych szkicach."""
        return self.store.reserved_numbers() + self.store.draft_numbers(exclude=exclude_draft)

    def save_draft(
        self,
        draft: Draft,
        draft_id: int | None = None,
        *,
        fields: dict[str, Any] | None = None,
        source_ksef: str | None = None,
    ) -> int:
        """Zapisuje szkic faktury (z surowymi wartościami pól edytora, jeśli są). Zwraca jego numer w bazie."""
        fields = fields or {}
        payload: dict[str, Any] = {"draft": dump(draft)}
        if fields:
            payload["fields"] = fields
        correction = draft.correction.ksef_number if draft.correction else None
        return self.store.save_draft(
            draft_id,
            payload,
            number=str(fields.get("number") or draft.number).strip(),
            kind=draft.kind,
            buyer_name=str(fields.get("buyer_name") or draft.buyer.nazwa).strip(),
            gross=float(draft.gross),
            currency=str(fields.get("currency") or draft.currency).strip().upper() or "PLN",
            source_ksef=source_ksef or correction,
            env=self.profile.env,
            context_id=self.profile.context_id,
        )

    def start_new_invoice(self) -> None:
        from .invoice_screens import QuickInvoiceScreen, SummaryScreen

        profile = self.profile
        if not profile.contractors and not profile.items and not self.store.recent_buyer_invoices(1):
            self.open_editor(new_draft(profile, date.today(), self.numbers_in_use()))
            return

        def chosen(result: Draft | tuple[str, Draft] | None) -> None:
            if isinstance(result, tuple):
                self.open_editor(result[1])
            elif result is not None:
                self.push_screen(SummaryScreen(result))

        self.push_screen(QuickInvoiceScreen(), chosen)

    def open_editor(
        self,
        draft: Draft,
        *,
        draft_id: int | None = None,
        fields: dict[str, Any] | None = None,
        source_ksef: str | None = None,
    ) -> None:
        """Edycja faktury: „Sprawdź fakturę” przechodzi do podsumowania, „Zamknij” zostawia szkic."""
        from .editor import EditResult, EditScreen
        from .invoice_screens import SummaryScreen

        def edited(result: EditResult | None) -> None:
            if result is None:
                return
            if result.review and result.draft is not None:
                self.push_screen(SummaryScreen(result.draft, draft_id=result.draft_id))
            elif result.discarded:
                self.notify("Szkic porzucony.", timeout=3)
            elif result.draft_id is not None:
                self.notify(draft_saved_text(), timeout=6)
            self.refresh_main()

        self.push_screen(EditScreen(draft, draft_id=draft_id, fields=fields, source_ksef=source_ksef), edited)

    def open_draft(self, draft_id: int) -> None:
        """Otwiera szkic tam, gdzie go zostawiono: w edycji (z wpisanymi wartościami) albo w podsumowaniu."""
        from .invoice_screens import SummaryScreen

        row = self.store.draft(draft_id)
        if row is None:
            return
        payload = json.loads(row["payload"])
        try:
            draft = load_draft(payload["draft"])
        except (KeyError, TypeError, ValueError) as exc:
            self.notify(f"Nie udało się odczytać szkicu: {exc}", severity="error")
            return
        if payload.get("fields"):
            self.open_editor(draft, draft_id=draft_id, fields=payload["fields"], source_ksef=row["source_ksef"])
        else:
            self.push_screen(SummaryScreen(draft, draft_id=draft_id))

    def discard_draft(self, draft_id: int, then: Any = None) -> None:
        """Porzuca szkic po potwierdzeniu (to jedyne miejsce, w którym wpisane dane faktury znikają)."""
        row = self.store.draft(draft_id)
        if row is None:
            return
        what = " ".join(x for x in (row["number"], f"dla {row['buyer_name']}" if row["buyer_name"] else "") if x)

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.store.delete_draft(draft_id)
                self.notify("Szkic porzucony.", timeout=3)
                self.refresh_main()
                if then:
                    then()

        self.push_screen(
            ConfirmScreen(
                "Porzucić szkic?",
                f"Szkic faktury {escape(what)} zostanie usunięty. Tego nie można cofnąć.",
                confirm="Porzuć szkic",
            ),
            confirmed,
        )

    def back_to_main(self, select: str | None = None) -> None:
        """Zamyka ekrany nad listą faktur (podsumowanie, podgląd) i zaznacza wskazaną fakturę."""
        from .main_screen import MainScreen

        while len(self.screen_stack) > 1 and not isinstance(self.screen, MainScreen):
            self.pop_screen()
        self.refresh_main(select=select)

    def refresh_main(self, select: str | None = None) -> None:
        from .main_screen import MainScreen

        for screen in self.screen_stack:
            # Ekran jeszcze niezamontowany (np. tuż po starcie) sam wczyta aktualne dane.
            if isinstance(screen, MainScreen) and screen.is_mounted:
                screen.refresh_counterparties()
                screen.reload(select=select)

    @work(exclusive=True, group="pending")
    async def reconcile_all(self, *, ask: bool = False) -> None:
        """Wysyłki bez wyniku (np. z poprzedniego uruchomienia) - sprawdzamy ich los w KSeF.

        Samo z siebie (po starcie) nie pyta o logowanie: w trybie lokalnym sprawdzenie poczeka do następnego razu.
        `ask` - sprawdzenie na prośbę użytkownika (np. „Sprawdź status” w Wysyłkach), więc wolno zapytać.
        """
        client = await self.get_client() if ask else self.client
        if client is None:
            return
        sessions: dict[str, list[dict[str, Any]]] = {}
        changed = False
        for row in self.store.unresolved_sent():
            if row["id"] in self.active_sends:
                continue
            number = row["invoice_number"]
            try:
                result = await reconcile(client, self.store, self.profile, row, sessions)
            except (RateLimitError, AuthError) as exc:
                self.notify(
                    f"{exc.human()}\nStatus wysyłek sprawdzimy przy następnym uruchomieniu.",
                    title="Nie udało się sprawdzić wysyłek",
                    severity="warning",
                    timeout=10,
                )
                break
            changed = changed or result.changed
            if result.status == "accepted":
                text = f"Faktura {number} została przyjęta.\nNumer KSeF: {result.ksef_number}"
                if result.exported and result.exported.error:
                    text += f"\nNie zapisano pliku w {short_path(result.exported.folder)}: {result.exported.error}"
                self.notify(text, title="KSeF", timeout=10)
            elif result.status == "rejected":
                self.notify(f"KSeF odrzucił fakturę {number}:\n{result.message}", severity="error", timeout=15)
            elif result.status == "not_submitted" and result.changed:
                self.notify(f"Faktura {number}: {result.message}", severity="warning", timeout=15)
        if changed:
            self.refresh_main()

    # ------------------------------------------------------- paleta poleceń

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        from . import account
        from .main_screen import MainScreen

        if self._store is None:
            yield from self._basic_commands()
            return
        commands = [SystemCommand("Nowa faktura", "Wystaw fakturę i wyślij ją do KSeF", self.start_new_invoice)]
        if isinstance(screen, MainScreen):
            commands += [
                SystemCommand("Synchronizuj z KSeF", "Pobierz nowe faktury z KSeF", screen.action_sync),
                SystemCommand("Wyślij pliki XML", "Wyślij gotowe faktury FA(3) z dysku", screen.action_import),
                SystemCommand("Pobierz fakturę po numerze KSeF", "Ściągnij dowolną fakturę", screen.action_fetch),
                SystemCommand("Pobierz starsze faktury", "Faktury sprzed daty synchronizacji", screen.action_backfill),
            ]
        commands += [
            SystemCommand("Wysyłki i UPO", "Stan faktur wysłanych z tej aplikacji, pobieranie UPO", self.open_sent),
            SystemCommand(
                "Sesje w KSeF", "Sesje wysyłki w KSeF (także z innych programów), UPO sesji", self._open_sessions
            ),
        ]
        for title, help_text, factory in account.SECTIONS:
            if factory is account.TestDataScreen and self.profile.env != "test":
                continue
            commands.append(SystemCommand(title, help_text, lambda f=factory: self.push_screen(f())))
        commands += [
            SystemCommand("Edytuj profil", f"Otwórz {short_path(self.profile.path)} w edytorze", self.edit_profile),
            SystemCommand(
                "Folder zapisu plików",
                f"Gdzie zapisywać XML i UPO (teraz {short_path(self.export_dir)})",
                self.choose_export_dir,
            ),
        ]
        if self.profile.auth.metoda == "token":
            commands.append(
                SystemCommand("Zmień token KSeF", "Podaj nowy token (np. po jego unieważnieniu)", self.change_token)
            )
        commands.append(
            SystemCommand("Wyloguj z KSeF", "Praca lokalna, koniec sesji, usunięcie zapisanego tokena", self.logout)
        )
        # Polecenie z palety może przykryć edytor faktury - wpisane dane najpierw trafiają do szkicu.
        for command in [*commands, *self._basic_commands()]:
            yield command._replace(callback=self._after_saving_drafts(command.callback))

    def _basic_commands(self) -> list[SystemCommand]:
        return [
            SystemCommand("Pomoc i skróty klawiszowe", "Co można zrobić na tym ekranie (F1)", self.action_help),
            SystemCommand("Motyw", "Zmień kolory aplikacji", self.action_change_theme),
            SystemCommand("Zakończ", "Zamknij aplikację (szkice zostają zapisane)", self.action_quit),
        ]

    def _after_saving_drafts(self, callback: Callable[[], Any]) -> Callable[[], Any]:
        from .editor import EditScreen

        def run() -> Any:
            for screen in self.screen_stack:
                if isinstance(screen, EditScreen):
                    screen.flush()
            return callback()

        return run

    def action_command_palette(self) -> None:
        if self.use_command_palette and not CommandPalette.is_open(self):
            placeholder = "Wpisz, czego szukasz: faktura, UPO, token, profil…"
            self.push_screen(CommandPalette(id="--command-palette", placeholder=placeholder))

    def action_change_theme(self) -> None:
        self.push_screen(CommandPalette(providers=[ThemeProvider], placeholder="Szukaj motywu…"))

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen(self.screen))

    def open_sent(self) -> None:
        from .sent_screen import SentScreen

        if not isinstance(self.screen, SentScreen):
            self.push_screen(SentScreen())

    def _open_sessions(self) -> None:
        from .account import SessionsScreen

        self.push_screen(SessionsScreen())

    def change_token(self) -> None:
        def entered(client: KsefClient | None) -> None:
            if client is not None:
                old, self.client = self.client, client
                if old:
                    self.run_worker(old.close())

        self.push_screen(TokenScreen(first_run=False), entered)

    def logout(self) -> None:
        """Wylogowanie z wyborem skutku: każda opcja robi dokładnie to, co opisuje (AT-29)."""
        profile = self.profile
        token = profile.auth.metoda == "token"
        again = "zapisanym tokenem" if token else "certyfikatem"
        choices: list[tuple[str, str, Any]] = [
            (
                "Pracuj lokalnie",
                "Rozłącza aplikację z KSeF. Faktury, wyszukiwanie i szkice działają dalej; "
                f"przy pierwszej operacji w KSeF aplikacja połączy się ponownie {again}.",
                "local",
            ),
            (
                "Zakończ bieżącą sesję",
                f"Unieważnia sesję logowania w KSeF. Następna operacja zaloguje się od nowa {again}.",
                "session",
            ),
        ]
        if token:
            where = storage_label(profile.env, profile.context_id).replace(str(Path.home()), "~")
            warning = " Zmienna KSEF_TOKEN nadal będzie podawać token." if env_token_set() else ""
            choices += [
                (
                    "Zapomnij token na tym urządzeniu",
                    f"Usuwa token zapisany {where} i rozłącza aplikację. W KSeF token nadal działa.{warning}",
                    "forget",
                ),
                (
                    "Unieważnij token w KSeF",
                    "Otwiera listę tokenów - unieważniony token przestaje działać wszędzie.",
                    "revoke",
                ),
            ]

        def chosen(choice: str | None) -> None:
            if choice == "local":
                self._disconnect("login" if self.connection == "login" else "local")
                self.notify(f"Pracujesz lokalnie. Połączysz się ponownie {again} przy operacji w KSeF.", timeout=6)
            elif choice == "session":
                self._end_session()
            elif choice == "forget":
                self._forget_token()
            elif choice == "revoke":
                from .account import TokensScreen

                self.push_screen(TokensScreen())

        self.push_screen(ChoiceScreen("Wyloguj z KSeF", choices), chosen)

    def _disconnect(self, state: str) -> None:
        old, self._client = self._client, None
        self.connection = state
        if old is not None:
            self.run_worker(old.close(), group="close-client")

    @work(exclusive=True, group="logout")
    async def _end_session(self) -> None:
        if self.client is None:
            self.notify("Aplikacja nie jest teraz połączona z KSeF - nie ma sesji do zakończenia.", timeout=5)
            return
        try:
            await self.client.revoke_current_session()
            self.notify("Sesja w KSeF została unieważniona.", timeout=4)
        except KsefError as exc:
            self.notify(exc.human(), title="Wylogowanie", severity="error")

    def _forget_token(self) -> None:
        profile = self.profile

        def confirmed(ok: bool | None) -> None:
            if not ok:
                return
            delete_token(profile.env, profile.context_id)
            self._disconnect("login")
            text = "Token usunięty z tego urządzenia. Aby połączyć się z KSeF, wklej token."
            if env_token_set():
                text += "\nZmienna KSEF_TOKEN nadal podaje token - przy następnym uruchomieniu aplikacja go użyje."
            self.notify(text, title="Wylogowano", timeout=10)

        self.push_screen(
            ConfirmScreen(
                "Zapomnieć token?",
                "Token zostanie usunięty z tego komputera, a aplikacja rozłączy się z KSeF.\n\n"
                "[$text-warning]KSeF nie pokazuje tokenów ponownie[/] - jeśli nie masz go zapisanego gdzie indziej, "
                "do połączenia wygenerujesz nowy.",
                confirm="Zapomnij token",
            ),
            confirmed,
        )

    def edit_profile(self) -> None:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        path = self.profile.path
        try:
            with self.suspend():
                subprocess.run([*shlex.split(editor), str(path)], check=False)
        except (OSError, SuspendNotSupported) as exc:
            self.notify(f"Nie udało się uruchomić edytora {editor!r}: {exc}", severity="error")
            return
        self.reload_profile()

    def reload_profile(self, *, quiet: bool = False) -> None:
        """Wczytuje profil po zmianach. Ta sama firma - od razu; inna firma, środowisko lub logowanie - po zgodzie."""
        from .main_screen import MainScreen

        try:
            profile = load_profile(self.profile.path, self.env_override)
        except ProfileError as exc:
            self.notify(
                str(exc), title="Profil zawiera błąd - zmiany nie zostały wczytane", severity="error", timeout=20
            )
            return
        old = self.profile
        if (profile.env, profile.context_id, profile.auth) != (old.env, old.context_id, old.auth):
            self._offer_switch(profile)
            return
        self._profile = profile
        self.theme = profile.theme
        for screen in self.screen_stack:
            if isinstance(screen, MainScreen) and screen.is_mounted:
                screen.rebuild()  # np. inne rodzaje faktur do synchronizacji albo nowa nazwa firmy
        if not quiet:
            self.notify("Wczytano zmiany w profilu.", title="Profil", timeout=4)

    def _offer_switch(self, profile: Profile) -> None:
        """Profil wskazuje inną firmę, środowisko albo sposób logowania - przełączamy dopiero po zgodzie."""
        old = self.profile
        changes = []
        if profile.env != old.env:
            changes.append(f"środowisko: {old.env_label} → {profile.env_label}")
        if profile.context_id != old.context_id:
            changes.append(f"firma (kontekst): {old.context_id} → {profile.context_id}")
        if profile.auth != old.auth:
            changes.append("sposób logowania")

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.switch_profile(profile)
            else:
                self.notify("Aplikacja pracuje dalej na poprzednich ustawieniach profilu.", timeout=6)

        self.push_screen(
            ConfirmScreen(
                "Przełączyć na nowy profil?",
                "Zmieniono w profilu:\n"
                + "\n".join(f"• {escape(c)}" for c in changes)
                + "\n\nAplikacja zapisze szkice, zamknie otwarte ekrany, rozłączy się z KSeF "
                "i otworzy faktury według nowego profilu.",
                confirm="Przełącz teraz",
                danger=False,
                focus_cancel=False,
            ),
            confirmed,
        )

    def switch_profile(self, profile: Profile) -> None:
        """Przełączenie firmy, środowiska albo logowania bez zamykania aplikacji."""
        from .editor import EditScreen
        from .import_screen import ImportScreen
        from .invoice_screens import SendScreen

        if any(
            (isinstance(s, SendScreen) and not s.finished) or (isinstance(s, ImportScreen) and s.sending)
            for s in self.screen_stack
        ):
            self.notify("Trwa wysyłka do KSeF - przełącz profil po jej zakończeniu.", severity="warning", timeout=8)
            return
        for screen in self.screen_stack:
            if isinstance(screen, EditScreen):
                screen.flush()
        self.workers.cancel_group(self, "pending")
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self._disconnect("login")
        if self._store is not None:
            self._store.close()
            self._store = None
        self._profile = profile
        self.theme = profile.theme
        self.sync_blocked_until = None
        self.notice = f"Profil: {profile.seller.nazwa} · {profile.env_label}"
        self._profile_ready()

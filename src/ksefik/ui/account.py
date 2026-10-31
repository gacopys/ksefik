"""Konto KSeF: sesje i UPO, tokeny, sesje logowania, limity, identyfikatory zbiorcze, Peppol i dane testowe."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.markup import escape
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from ..client import KsefClient, KsefError, TokenCredentials
from ..codes import AUTH_METHODS, PERMISSION_LABELS, TOKEN_PERMISSIONS, TOKEN_STATUSES
from ..money import fmt_amount
from ..store import Filters, InvoiceRow
from ..tokenstore import env_token_set, save_token
from .common import (
    OUTCOME,
    ConfirmScreen,
    FormField,
    FormScreen,
    ListScreen,
    check_description,
    check_nip,
    check_pesel,
    error_text,
    fmt_dt,
    fmt_ident,
    short_path,
    topbar,
)
from .import_screen import KSEF_NUMBER

if TYPE_CHECKING:
    from .app import KsefApp


def perms(values: Sequence[str]) -> str:
    return ", ".join(PERMISSION_LABELS.get(v, v) for v in values)


# -------------------------------------------------------------------- sesje i UPO


class SessionsScreen(ListScreen):
    TITLE_TEXT = "Sesje w KSeF"
    DESCRIPTION = (
        "Sesje wysyłki faktur w KSeF z ostatnich tygodni - także z innych programów. "
        "Enter - faktury sesji, u - UPO zbiorcze sesji."
    )
    COLUMNS = [("Utworzona", 16), ("Status", 28), ("Przyjęte", 9), ("Błędy", 6), ("Numer referencyjny", None)]
    EMPTY = "Brak sesji w wybranym okresie."
    REQUIRES = "Lista sesji wymaga uprawnienia do wystawiania faktur (InvoiceWrite) albo Introspection."
    BINDINGS = [
        *ListScreen.BINDINGS,
        Binding("u", "session_upo", "UPO sesji"),
        Binding("m", "more", "Wczytaj starsze"),
    ]
    PAGE = 500

    def __init__(self) -> None:
        super().__init__()
        self.limit = self.PAGE
        self.truncated = False

    def compose_header(self) -> ComposeResult:
        with Horizontal(classes="list-filters"):
            yield Label("Rodzaj", classes="filter-label first")
            options = [("interaktywne", "Online"), ("wsadowe", "Batch")]
            yield Select(options, value="Online", allow_blank=False, id="session-type", compact=True)
            yield Label("Okres", classes="filter-label")
            periods = [("7 dni", 7), ("30 dni", 30), ("90 dni", 90), ("rok", 365)]
            yield Select(periods, value=30, allow_blank=False, id="session-days", compact=True)

    @on(Select.Changed, ".list-filters Select")
    def _filters_changed(self) -> None:
        # Select zgłasza zmianę także przy pierwszym wyświetleniu - wtedy lista i tak się wczytuje.
        if self.is_mounted and self._filters() != self._loaded:
            self.action_reload()

    _loaded: tuple[str, int] | None = None

    def on_mount(self) -> None:
        self._loaded = self._filters()  # wczytuje je ListScreen.on_mount

    def _filters(self) -> tuple[str, int]:
        return str(self.query_one("#session-type", Select).value), int(self.query_one("#session-days", Select).value)

    async def fetch(self, client: KsefClient) -> list[dict[str, Any]]:
        self._loaded = self._filters()
        kind, days = self._loaded
        since = datetime.now(UTC) - timedelta(days=days)
        items = await client.list_sessions(kind, created_from=since, limit=self.limit + 1)
        self.truncated = len(items) > self.limit
        return items[: self.limit]

    def show_items(self, items: list[dict[str, Any]]) -> None:
        super().show_items(items)
        if self.truncated:
            self.show_outcome(
                f"[$text-warning]Pokazano {len(items)} najnowszych sesji z wybranego okresu - m wczytuje starsze.[/]"
            )
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "more":
            return self.truncated
        return super().check_action(action, parameters)

    def action_more(self) -> None:
        if self.truncated:
            self.limit += self.PAGE
            self.action_reload()

    def row(self, item: dict[str, Any]) -> Sequence[Any]:
        status = item["status"]
        style = "green" if status["code"] == 200 else "red" if status["code"] >= 400 else "yellow"
        return (
            fmt_dt(item.get("dateCreated")),
            Text(status.get("description", ""), style=style),
            Text(f"{item.get('successfulInvoiceCount', 0)}/{item.get('totalInvoiceCount', 0)}", justify="right"),
            Text(str(item.get("failedInvoiceCount", 0)), justify="right"),
            item["referenceNumber"],
        )

    def details(self, item: dict[str, Any]) -> str:
        parts = [f"[$foreground-muted]Ostatnia aktywność:[/] {fmt_dt(item.get('dateUpdated'))}"]
        if item.get("validUntil"):
            parts.append(f"[$foreground-muted]ważna do:[/] {fmt_dt(item['validUntil'])}")
        details = item["status"].get("details") or []
        text = "   ".join(parts)
        if details:
            text += "\n" + "\n".join(f"• {escape(d)}" for d in details)
        return text

    @on(DataTable.RowSelected, "#list")
    def _open(self) -> None:
        if item := self.selected:
            self.app.push_screen(SessionInvoicesScreen(item["referenceNumber"]))

    @work(exclusive=True, group="op")
    async def action_session_upo(self) -> None:
        item = self.selected
        if item is None:
            return
        client = await self.app.get_client()
        if client is None:
            return
        reference = item["referenceNumber"]
        try:
            status = await client.session_status(reference)
        except KsefError as exc:
            self.notify(exc.human(), title="UPO sesji", severity="error")
            return
        pages = (status.get("upo") or {}).get("pages") or []
        if not pages:
            self.notify("UPO sesji jest generowane po jej zamknięciu - spróbuj później.", severity="warning")
            return
        files, failed = [], []
        for page in pages:  # błąd jednej strony nie przekreśla pozostałych
            try:
                upo = await client.session_upo(reference, page["referenceNumber"])
            except KsefError as exc:
                failed.append(f"{page['referenceNumber']}: {exc.message}")
                continue
            files.append((f"UPO_{reference}_{page['referenceNumber']}.xml", upo))
        if failed:
            self.show_outcome(
                f"[$text-warning]UPO sesji {escape(reference)}: pobrano {len(files)} z {len(pages)} stron. "
                f"Nie udało się: {escape('; '.join(failed))}. Naciśnij u, aby ponowić.[/]"
            )
        if files:
            self.app.export_files(files, title="UPO sesji")


class SessionInvoicesScreen(ListScreen):
    TITLE_TEXT = "Faktury sesji"
    COLUMNS = [("Lp.", 4), ("Numer faktury", 22), ("Numer KSeF", 38), ("Status", None)]
    EMPTY = "Sesja nie zawiera faktur."
    BINDINGS = [
        *ListScreen.BINDINGS,
        Binding("u", "upo", "Pobierz UPO"),
        Binding("f", "toggle_failed", "Tylko błędne"),
    ]

    def __init__(self, session_ref: str) -> None:
        super().__init__()
        self.session_ref = session_ref
        self.failed_only = False
        self.TITLE_TEXT = f"Faktury sesji {session_ref}"

    async def fetch(self, client: KsefClient) -> list[dict[str, Any]]:
        return await client.session_invoices(self.session_ref, failed_only=self.failed_only)

    def row(self, item: dict[str, Any]) -> Sequence[Any]:
        status = item["status"]
        style = "green" if status["code"] == 200 else "red" if status["code"] >= 400 else "yellow"
        return (
            str(item.get("ordinalNumber", "")),
            item.get("invoiceNumber") or item.get("invoiceFileName") or "",
            item.get("ksefNumber") or "",
            Text(status.get("description", ""), style=style),
        )

    def details(self, item: dict[str, Any]) -> str:
        lines = [
            f"[$foreground-muted]Numer referencyjny:[/] {item['referenceNumber']}   "
            f"[$foreground-muted]przyjęta:[/] {fmt_dt(item.get('invoicingDate'))}   "
            f"[$foreground-muted]numer KSeF nadany:[/] {fmt_dt(item.get('acquisitionDate'))}"
        ]
        lines += [f"• {escape(d)}" for d in item["status"].get("details") or []]
        return "\n".join(lines)

    def action_toggle_failed(self) -> None:
        self.failed_only = not self.failed_only
        self.EMPTY = "W tej sesji nie ma faktur z błędami." if self.failed_only else type(self).EMPTY
        self.notify("Tylko faktury z błędami." if self.failed_only else "Wszystkie faktury sesji.", timeout=2)
        self.action_reload()

    @work(exclusive=True, group="op")
    async def action_upo(self) -> None:
        item = self.selected
        if item is None or item["status"]["code"] != 200:
            self.notify("UPO jest dostępne tylko dla przyjętych faktur.", severity="warning")
            return
        client = await self.app.get_client()
        if client is None:
            return
        try:
            upo = await client.upo_by_reference(self.session_ref, item["referenceNumber"])
        except KsefError as exc:
            self.notify(exc.human(), title="UPO", severity="error")
            return
        self.app.export_file(f"UPO_{item.get('ksefNumber') or item['referenceNumber']}.xml", upo, title="UPO")


# ------------------------------------------------------------------------- tokeny


class TokenCreatedScreen(ModalScreen[None]):
    """Nowy token pokazywany jeden raz. Okno czeka na jego aktywację w KSeF i nie zamyka się przez przypadek."""

    BINDINGS = [Binding("escape", "close", "Zamknij")]

    app: KsefApp

    def __init__(self, token: str, description: str, reference: str) -> None:
        super().__init__()
        self.token = token
        self.description = description
        self.reference = reference
        self.state = "pending"  # pending | active | failed | unknown
        self.problem = ""
        self.kept = False  # token skopiowany albo zapisany w aplikacji

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog secret-dialog"):
            yield Static("[b]Nowy token KSeF[/b]", classes="dialog-title")
            yield Static(
                f"„{escape(self.description)}”\n\n[b $text-warning]Token jest pokazywany tylko raz.[/] "
                "Skopiuj go teraz albo użyj w tej aplikacji.",
                classes="dialog-text",
            )
            yield Static(escape(self.token), id="secret-value")
            yield Static("", id="token-state", classes="dialog-text")
            with Horizontal(classes="dialog-buttons"):
                if self.app.profile.auth.metoda == "token":
                    yield Button("Używaj w tej aplikacji", id="use", disabled=True)
                yield Button("Kopiuj", variant="primary", id="copy")
                yield Button("Zamknij", id="close")

    def on_mount(self) -> None:
        self.query_one("#copy", Button).focus()
        self._show_state()
        self.activate()

    @work(exclusive=True)
    async def activate(self) -> None:
        """Czeka, aż KSeF aktywuje token - wcześniej nie da się nim zalogować."""
        client = self.app.client
        if client is None:
            self.state, self.problem = "unknown", "brak połączenia z KSeF"
            self._show_state()
            return
        try:
            status = await client.wait_for_token(self.reference)
        except KsefError as exc:
            self.state, self.problem = "unknown", exc.message
        else:
            if status.get("status") == "Active":
                self.state = "active"
            elif status.get("status") == "Pending":
                self.state, self.problem = "unknown", "token wciąż czeka na aktywację"
            else:
                self.state = "failed"
                self.problem = "; ".join(status.get("statusDetails") or []) or str(status.get("status", ""))
        self._show_state()

    def _show_state(self) -> None:
        texts = {
            "pending": "[$text-accent]⟳ KSeF aktywuje token (zwykle kilka sekund)…[/]",
            "active": "[$text-success]✓ Token jest aktywny.[/]",
            "failed": f"[$text-error]✗ KSeF nie aktywował tokena: {escape(self.problem)}[/]",
            "unknown": f"[$text-warning]Nie udało się potwierdzić aktywacji ({escape(self.problem)}). "
            "Stan tokena sprawdzisz na liście tokenów.[/]",
        }
        self.query_one("#token-state", Static).update(texts[self.state])
        with suppress(NoMatches):
            self.query_one("#use", Button).disabled = self.state != "active"

    @on(Button.Pressed, "#copy")
    def _copy(self) -> None:
        self.kept = True
        self.app.copy_text(self.token, "token")

    @on(Button.Pressed, "#use")
    def _use(self) -> None:
        if self.state != "active":
            return
        profile = self.app.profile
        where = save_token(profile.env, profile.context_id, self.token)
        self.kept = True
        old, self.app.client = self.app.client, self.app.make_client(TokenCredentials(self.token))
        if old:
            self.app.run_worker(old.close())
        text = f"Aplikacja używa nowego tokena (zapisano: {where})."
        if env_token_set():
            text += "\nZmienna KSEF_TOKEN ma pierwszeństwo - przy następnym uruchomieniu aplikacja użyje tokena z niej."
        self.notify(text, timeout=8)
        self.dismiss(None)

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        if self.kept:
            self.dismiss(None)
            return

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.dismiss(None)

        self.app.push_screen(
            ConfirmScreen(
                "Zamknąć bez zapisania tokena?",
                "Ten token jest widoczny tylko teraz. Po zamknięciu okna nie da się go odczytać - "
                "trzeba będzie wygenerować nowy.",
                confirm="Zamknij świadomie",
                cancel="Wróć",
            ),
            confirmed,
        )


class TokensScreen(ListScreen):
    TITLE_TEXT = "Tokeny KSeF"
    DESCRIPTION = "Tokeny służą do logowania programów (także tej aplikacji). n - nowy token, Del - unieważnij."
    COLUMNS = [("Opis", 30), ("Status", 12), ("Uprawnienia", 44), ("Utworzony", 16), ("Ostatnio użyty", 16)]
    EMPTY = "Brak tokenów w tym kontekście."
    REQUIRES = "Pełna lista tokenów wymaga uprawnienia CredentialsRead lub CredentialsManage."
    BINDINGS = [
        *ListScreen.BINDINGS,
        Binding("n", "new", "Nowy token"),
        Binding("delete", "revoke", "Unieważnij", key_display="Del"),
    ]

    async def fetch(self, client: KsefClient) -> list[dict[str, Any]]:
        return await client.list_tokens()

    def row(self, item: dict[str, Any]) -> Sequence[Any]:
        status = item["status"]
        style = {"Active": "green", "Pending": "yellow", "Failed": "red"}.get(status, "dim")
        return (
            item.get("description", ""),
            Text(TOKEN_STATUSES.get(status, status), style=style),
            perms(item.get("requestedPermissions") or []),
            fmt_dt(item.get("dateCreated")),
            fmt_dt(item.get("lastUseDate")) or "—",
        )

    def details(self, item: dict[str, Any]) -> str:
        text = (
            f"[$foreground-muted]Numer:[/] {item['referenceNumber']}   [$foreground-muted]autor:[/] "
            f"{escape(fmt_ident(item.get('authorIdentifier')))}   [$foreground-muted]kontekst:[/] "
            f"{escape(fmt_ident(item.get('contextIdentifier')))}"
        )
        for detail in item.get("statusDetails") or []:
            text += f"\n• {escape(detail)}"
        return text

    def action_new(self) -> None:
        options = [(PERMISSION_LABELS[p], p) for p in TOKEN_PERMISSIONS]
        form = FormScreen(
            "Nowy token KSeF",
            [
                FormField("description", "Opis (np. nazwa programu)", required=True, check=check_description),
                FormField("permissions", "Uprawnienia tokena", "multi", ["InvoiceRead", "InvoiceWrite"], options),
            ],
            intro="Token pozwoli zalogować się do KSeF w kontekście "
            f"[b]{escape(fmt_ident({'type': self.app.profile.context_type, 'value': self.app.profile.context_id}))}"
            "[/b] z wybranymi uprawnieniami.",
            submit="Wygeneruj token",
            validate=lambda v: None if v["permissions"] else ("permissions", "wybierz co najmniej jedno uprawnienie"),
            on_submit=self.form_operation(
                "Nowy token",
                lambda c, v: c.generate_token(v["permissions"], v["description"]),
                success="Token wygenerowany - aktywuje się w ciągu kilku sekund.",
            ),
        )

        def created(values: dict[str, Any] | None) -> None:
            outcome = values.get(OUTCOME) if values else None
            if values and outcome and outcome.ok:
                created = outcome.value
                screen = TokenCreatedScreen(created["token"], values["description"], created["referenceNumber"])
                self.app.push_screen(screen, lambda _: self.action_reload())  # status tokena mógł się zmienić

        self.app.push_screen(form, created)

    def action_revoke(self) -> None:
        item = self.selected
        if item is None:
            return
        if item["status"] not in ("Active", "Pending"):
            self.notify("Ten token jest już nieaktywny.", timeout=3)
            return

        def confirmed(ok: bool | None) -> None:
            if ok:
                self._revoke(item["referenceNumber"])

        self.app.push_screen(
            ConfirmScreen(
                "Unieważnić token?",
                f"„{escape(item.get('description', ''))}”\n\nProgramy korzystające z tego tokena stracą dostęp "
                "do KSeF. Tej operacji nie można cofnąć.",
                confirm="Unieważnij",
            ),
            confirmed,
        )

    @work(exclusive=True, group="op")
    async def _revoke(self, reference: str) -> None:
        await self.run_operation(
            "Unieważnienie tokena", lambda c: c.revoke_token(reference), success="Token unieważniony."
        )


# ---------------------------------------------------------------- sesje logowania


class AuthSessionsScreen(ListScreen):
    TITLE_TEXT = "Sesje logowania"
    DESCRIPTION = "Aktywne logowania do KSeF w tym kontekście. Del - unieważnij wybraną sesję."
    COLUMNS = [("Rozpoczęta", 16), ("Metoda", 24), ("Status", 34), ("Odświeżanie do", 16), ("", 10)]
    EMPTY = "Brak aktywnych sesji."
    BINDINGS = [*ListScreen.BINDINGS, Binding("delete", "revoke", "Unieważnij", key_display="Del")]

    async def fetch(self, client: KsefClient) -> list[dict[str, Any]]:
        return await client.auth_sessions()

    def row(self, item: dict[str, Any]) -> Sequence[Any]:
        info = item.get("authenticationMethodInfo") or {}
        method = info.get("displayName") or AUTH_METHODS.get(item.get("authenticationMethod", ""), "")
        return (
            fmt_dt(item.get("startDate")),
            method,
            item["status"].get("description", ""),
            fmt_dt(item.get("refreshTokenValidUntil")),
            Text("bieżąca", style="bold green") if item.get("isCurrent") else "",
        )

    def details(self, item: dict[str, Any]) -> str:
        return (
            f"[$foreground-muted]Numer:[/] {item['referenceNumber']}   "
            f"[$foreground-muted]ostatnie odświeżenie:[/] {fmt_dt(item.get('lastTokenRefreshDate')) or '—'}"
        )

    def action_revoke(self) -> None:
        item = self.selected
        if item is None:
            return
        current = bool(item.get("isCurrent"))
        message = (
            "To sesja tej aplikacji - przy następnej operacji aplikacja zaloguje się ponownie."
            if current
            else "Program korzystający z tej sesji straci dostęp do KSeF do czasu ponownego zalogowania."
        )

        def confirmed(ok: bool | None) -> None:
            if ok:
                self._revoke(item["referenceNumber"], current)

        self.app.push_screen(ConfirmScreen("Unieważnić sesję logowania?", message, confirm="Unieważnij"), confirmed)

    @work(exclusive=True, group="op")
    async def _revoke(self, reference: str, current: bool) -> None:
        async def revoke(client: KsefClient) -> None:
            if current:
                await client.revoke_current_session()
            else:
                await client.revoke_auth_session(reference)

        await self.run_operation("Sesja logowania", revoke, success="Sesja unieważniona.")


# --------------------------------------------------------------------------- limity

RATE_LIMIT_LABELS = {
    "onlineSession": "Otwarcie sesji interaktywnej",
    "onlineSessionClose": "Zamknięcie sesji interaktywnej",
    "batchSession": "Otwarcie sesji wsadowej",
    "batchSessionClose": "Zamknięcie sesji wsadowej",
    "invoiceSend": "Wysłanie faktury",
    "invoiceStatus": "Status faktury",
    "sessionList": "Lista sesji",
    "sessionInvoiceList": "Faktury sesji",
    "sessionMisc": "Pozostałe operacje sesji",
    "invoiceMetadata": "Metadane faktur",
    "invoiceExport": "Eksport faktur",
    "invoiceExportStatus": "Status eksportu",
    "invoiceDownload": "Pobranie faktury",
    "collectiveIdentifier": "Identyfikatory zbiorcze",
    "other": "Pozostałe",
    "anonymous": "Bez logowania",
    "global": "Łącznie",
}


class LimitsScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Wróć"), Binding("r", "reload", "Odśwież")]

    app: KsefApp

    def compose(self) -> ComposeResult:
        yield from topbar("[b]Limity KSeF[/b]", self.app)
        with VerticalScroll(id="limits"):
            yield Static("[$foreground-muted]Wczytuję limity…[/]", id="limits-summary", classes="card")
            table = DataTable(cursor_type="none", zebra_stripes=True, id="rate-limits", classes="doc-table")
            table.border_title = "Limity zapytań API"
            yield table
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#rate-limits", DataTable)
        table.add_column("Operacja")
        for label in ("na sekundę", "na minutę", "na godzinę"):
            table.add_column(Text(label, justify="right"))
        self.query_one("#limits-summary").border_title = "Limity sesji i certyfikatów"
        self.action_reload()

    @work(exclusive=True)
    async def action_reload(self) -> None:
        summary = self.query_one("#limits-summary", Static)
        client = await self.app.get_client()
        if client is None:
            return
        try:
            context = await client.context_limits()
            subject = await client.subject_limits()
            rates = await client.rate_limits()
        except KsefError as exc:
            summary.update(error_text(exc))
            return
        online, batch = context.get("onlineSession", {}), context.get("batchSession", {})
        collective = context.get("collectiveIdentifier", {})

        def session(name: str, data: dict[str, Any]) -> str:
            return (
                f"[b]{name}[/b]: do [b]{data.get('maxInvoices', '?')}[/b] faktur w sesji, faktura do "
                f"[b]{data.get('maxInvoiceSizeInMB', '?')} MB[/b] "
                f"(z załącznikiem {data.get('maxInvoiceWithAttachmentSizeInMB', '?')} MB)"
            )

        lines = [
            session("Sesja interaktywna", online),
            session("Sesja wsadowa", batch),
            f"[b]Identyfikator zbiorczy[/b]: do {collective.get('maxInvoices', '?')} faktur",
            f"[b]Certyfikaty KSeF[/b]: do {subject.get('certificate', {}).get('maxCertificates', '?')} certyfikatów, "
            f"{subject.get('enrollment', {}).get('maxEnrollments', '?')} wniosków",
        ]
        summary.update("\n".join(lines))
        table = self.query_one("#rate-limits", DataTable)
        table.clear()
        for key, values in rates.items():
            table.add_row(
                RATE_LIMIT_LABELS.get(key, key),
                *(Text(str(values.get(k, "")), justify="right") for k in ("perSecond", "perMinute", "perHour")),
            )


class PeppolScreen(ListScreen):
    TITLE_TEXT = "Dostawcy usług Peppol"
    DESCRIPTION = "Dostawcy Peppol zarejestrowani w KSeF (faktury PEF)."
    COLUMNS = [("Identyfikator", 12), ("Nazwa", None), ("Zarejestrowany", 16)]

    async def fetch(self, client: KsefClient) -> list[dict[str, Any]]:
        return await client.peppol_providers()

    def row(self, item: dict[str, Any]) -> Sequence[Any]:
        return item["id"], item.get("name", ""), fmt_dt(item.get("dateCreated"))


# ----------------------------------------------------------- identyfikatory zbiorcze


COLLECTIVE_DAYS = 120


def _ksef_numbers(text: str | None) -> list[str]:
    return [n for n in re.split(r"[\s,;]+", (text or "").upper()) if n]


def _invoice_option(row: InvoiceRow) -> str:
    """Wiersz wyboru faktury w stałych kolumnach - mieści się w oknie 80 znaków."""
    day = f"{row.issue_date:%d.%m}" if row.issue_date else "     "
    name = row.counterparty_name or ""
    name = name if len(name) <= 20 else name[:19] + "…"
    return f"{day}  {(row.invoice_number or '—')[:14]:<14}  {name:<20}  {fmt_amount(row.gross):>12} {row.currency}"


def _chosen(values: dict[str, Any]) -> list[str]:
    """Faktury do identyfikatora: zaznaczone i wpisane, bez powtórzeń, w kolejności wyboru."""
    return list(dict.fromkeys([*(values.get("chosen") or []), *_ksef_numbers(values.get("numbers"))]))


class CollectiveScreen(ListScreen):
    TITLE_TEXT = "Identyfikatory zbiorcze"
    DESCRIPTION = (
        "Jeden identyfikator płatności dla wielu faktur (ostatnie 90 dni). n - nowy, Enter - faktury identyfikatora."
    )
    COLUMNS = [("Identyfikator", 38), ("Utworzony", 16), ("Faktur", 7), ("Utworzony tutaj", None)]
    EMPTY = "Brak identyfikatorów zbiorczych z ostatnich 90 dni."
    REQUIRES = "Wymaga uprawnienia do faktur (InvoiceRead/InvoiceWrite) lub CollectiveIdentifierManage."
    BINDINGS = [*ListScreen.BINDINGS, Binding("n", "new", "Nowy identyfikator")]

    async def fetch(self, client: KsefClient) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        return await client.query_collective_identifiers(now - timedelta(days=90), now)

    def row(self, item: dict[str, Any]) -> Sequence[Any]:
        return (
            item["collectiveIdentifierNumber"],
            fmt_dt(item.get("dateCreated")),
            Text(str(item.get("invoiceCount", 0)), justify="right"),
            "tak" if item.get("createdInCurrentContext") else "nie",
        )

    @on(DataTable.RowSelected, "#list")
    def _open(self) -> None:
        if item := self.selected:
            self._show_invoices(item["collectiveIdentifierNumber"])

    @work(exclusive=True, group="op")
    async def _show_invoices(self, number: str) -> None:
        client = await self.app.get_client()
        if client is None:
            return
        try:
            invoices = await client.collective_identifier_invoices([number])
        except KsefError as exc:
            self.notify(exc.human(), severity="error")
            return
        lines = []
        for inv in invoices:
            payment = inv.get("payment") or {}
            amount = f"  {fmt_amount(payment['amount'])} {payment.get('currency', '')}" if payment else ""
            lines.append(f"{inv['ksefNumber']}{amount}")
        self.query_one("#list-details", Static).update(
            f"[b]{escape(number)}[/b]\n" + ("\n".join(escape(line) for line in lines) or "brak faktur")
        )

    def action_new(self) -> None:
        """Faktury wybiera się z lokalnej listy (bez przepisywania numerów); inne można dopisać ręcznie."""
        recent = self.app.store.find_invoices(
            Filters(date_from=date.today() - timedelta(days=COLLECTIVE_DAYS)), limit=300
        ).rows
        known = {r.ksef_number: r for r in recent}
        options = [(_invoice_option(r), r.ksef_number) for r in recent]

        def check(value: str) -> str | None:
            bad = [n for n in _ksef_numbers(value) if not KSEF_NUMBER.match(n)]
            return f"niepoprawny numer KSeF: {bad[0]}" if bad else None

        def validate(values: dict[str, Any]) -> tuple[str, str] | None:
            if len(_chosen(values)) < 2:
                return "chosen", "wybierz co najmniej dwie faktury (z listy albo wpisane numery)"
            return None

        def review(values: dict[str, Any]) -> str:
            chosen = _chosen(values)
            totals: dict[str, float] = {}
            lines = []
            for number in chosen:
                row = known.get(number)
                if row is None:
                    lines.append(f"• {escape(number)} [$foreground-muted](spoza lokalnej bazy)[/]")
                    continue
                totals[row.currency] = totals.get(row.currency, 0) + row.gross
                lines.append(f"• {escape(row.invoice_number or number)}  {fmt_amount(row.gross)} {row.currency}")
            sums = " + ".join(f"{fmt_amount(v)} {c}" for c, v in totals.items())
            head = f"Faktur: [b]{len(chosen)}[/b]" + (f" · razem [b]{sums}[/b]" if sums else "")
            return head + "\n\n" + "\n".join(lines)

        form = FormScreen(
            "Nowy identyfikator zbiorczy",
            [
                FormField("chosen", "Faktury z ostatnich 120 dni", "multi", [], options),
                FormField(
                    "numbers",
                    "Inne numery KSeF (opcjonalnie, po jednym w wierszu)",
                    "area",
                    "",
                    check=check,
                ),
                FormField("description", "Opis (opcjonalnie)", max_length=512),
            ],
            intro="Identyfikator zbiorczy pozwala zapłacić jednym przelewem za wiele faktur. "
            "Zaznacz faktury spacją; powtórzone numery liczą się raz.",
            submit="Wygeneruj",
            wide=True,
            validate=validate,
            review=review,
            on_submit=self.form_operation(
                "Identyfikator zbiorczy", self._generate, success="Identyfikator wygenerowany."
            ),
        )

        def created(values: dict[str, Any] | None) -> None:
            outcome = values.get(OUTCOME) if values else None
            if values and outcome and outcome.ok and isinstance(outcome.value, str):
                count = len(_chosen(values))
                self.show_outcome(
                    f"[$text-success]✓ Identyfikator zbiorczy [b]{escape(outcome.value)}[/b] dla {count} faktur.[/]"
                )
                self.app.copy_text(outcome.value, "identyfikator zbiorczy")

        self.app.push_screen(form, created)

    @staticmethod
    async def _generate(client: KsefClient, values: dict[str, Any]) -> Any:
        invoices = [{"ksefNumber": n} for n in _chosen(values)]
        return await client.generate_collective_identifier(invoices, values.get("description"))


# ---------------------------------------------------------------------- dane testowe

TEST_SUBJECT_TYPES = [("Organ egzekucyjny", "EnforcementAuthority"), ("Grupa VAT", "VatGroup"), ("JST", "JST")]
CONTEXT_TYPES = [("NIP", "Nip"), ("Identyfikator wewnętrzny", "InternalId"), ("NIP-VAT UE", "NipVatUe")]
IDENTIFIER_CHOICES = [("NIP", "Nip"), ("PESEL", "Pesel"), ("Odcisk certyfikatu", "Fingerprint")]


# Operacje, które coś usuwają, blokują albo przywracają - przed wykonaniem pytamy, z opisem skutku.
TEST_CONSEQUENCES = {
    "Usuń podmiot": "Podmiot testowy i jego role zostaną usunięte z KSeF.",
    "Usuń osobę fizyczną": "Osoba testowa zostanie usunięta z KSeF.",
    "Odbierz uprawnienia testowe": "Uprawniony straci dostęp w tym kontekście.",
    "Cofnij zgodę na załączniki": "Faktury z załącznikiem przestaną być przyjmowane dla tego NIP-u.",
    "Zablokuj kontekst": (
        "Logowanie w tym kontekście zostanie zablokowane - także w tej aplikacji, jeśli to Twój kontekst."
    ),
    "Przywróć domyślne limity sesji": "Ustawione limity sesji wrócą do wartości domyślnych KSeF.",
    "Ustaw produkcyjne limity API": (
        "Limity zapytań będą jak na produkcji (niższe) - częściej zobaczysz „limit zapytań”."
    ),
    "Przywróć domyślne limity API": "Limity zapytań wrócą do wartości domyślnych środowiska testowego.",
    "Przywróć domyślne limity certyfikatów": "Limity wniosków i certyfikatów wrócą do wartości domyślnych KSeF.",
}


def check_range(low: int, high: int) -> Callable[[str], str | None]:
    def check(value: str) -> str | None:
        return None if value.isdigit() and low <= int(value) <= high else f"liczba od {low} do {high}"

    return check


def _test_operations(nip: str) -> list[tuple[str, str, list[FormField], Any]]:
    """(tytuł, opis, pola formularza, funkcja budująca (operacja, body, metoda))."""
    nip_field = FormField("nip", "NIP", value=nip, required=True, check=check_nip, max_length=10)
    description = FormField("description", "Opis", value="Dane testowe ksefik", required=True, check=check_description)
    test_permissions = [(PERMISSION_LABELS[p], p) for p in TOKEN_PERMISSIONS]
    return [
        (
            "Utwórz podmiot",
            "JST, grupa VAT albo organ egzekucyjny (z rolą w KSeF)",
            [
                FormField("nip", "NIP podmiotu", required=True, check=check_nip, max_length=10),
                FormField("type", "Rodzaj", "select", "EnforcementAuthority", TEST_SUBJECT_TYPES, required=True),
                description,
            ],
            lambda v: ("subject", {"subjectNip": v["nip"], "subjectType": v["type"], "description": v["description"]}),
        ),
        (
            "Usuń podmiot",
            "",
            [FormField("nip", "NIP podmiotu", required=True, check=check_nip)],
            lambda v: ("subject/remove", {"subjectNip": v["nip"]}),
        ),
        (
            "Utwórz osobę fizyczną",
            "osoba z NIP-em i PESEL-em (np. do testów uprawnień)",
            [
                FormField("nip", "NIP", required=True, check=check_nip),
                FormField("pesel", "PESEL", required=True, check=check_pesel),
                FormField("bailiff", "Komornik sądowy", "checkbox", False),
                description,
            ],
            lambda v: (
                "person",
                {
                    "nip": v["nip"],
                    "pesel": v["pesel"],
                    "isBailiff": bool(v["bailiff"]),
                    "description": v["description"],
                },
            ),
        ),
        (
            "Usuń osobę fizyczną",
            "",
            [FormField("nip", "NIP", required=True, check=check_nip)],
            lambda v: ("person/remove", {"nip": v["nip"]}),
        ),
        (
            "Nadaj uprawnienia testowe",
            "bez podpisu i ZAW-FA - tylko środowisko testowe",
            [
                FormField("context", "NIP kontekstu", value=nip, required=True, check=check_nip),
                FormField(
                    "kind", "Uprawniony - rodzaj identyfikatora", "select", "Nip", IDENTIFIER_CHOICES, required=True
                ),
                FormField("value", "Uprawniony - identyfikator", required=True),
                FormField("permissions", "Uprawnienia", "multi", ["InvoiceRead", "InvoiceWrite"], test_permissions),
                description,
            ],
            lambda v: (
                "permissions",
                {
                    "contextIdentifier": {"type": "Nip", "value": v["context"]},
                    "authorizedIdentifier": {"type": v["kind"], "value": v["value"]},
                    "permissions": [{"permissionType": p, "description": v["description"]} for p in v["permissions"]],
                },
            ),
        ),
        (
            "Odbierz uprawnienia testowe",
            "",
            [
                FormField("context", "NIP kontekstu", value=nip, required=True, check=check_nip),
                FormField(
                    "kind", "Uprawniony - rodzaj identyfikatora", "select", "Nip", IDENTIFIER_CHOICES, required=True
                ),
                FormField("value", "Uprawniony - identyfikator", required=True),
            ],
            lambda v: (
                "permissions/revoke",
                {
                    "contextIdentifier": {"type": "Nip", "value": v["context"]},
                    "authorizedIdentifier": {"type": v["kind"], "value": v["value"]},
                },
            ),
        ),
        ("Zezwól na faktury z załącznikiem", "", [nip_field], lambda v: ("attachment", {"nip": v["nip"]})),
        (
            "Cofnij zgodę na załączniki",
            "",
            [nip_field, FormField("end", "Data wycofania (opcjonalnie)", "date")],
            lambda v: (
                "attachment/revoke",
                {"nip": v["nip"], **({"expectedEndDate": v["end"].isoformat()} if v["end"] else {})},
            ),
        ),
        (
            "Zablokuj kontekst",
            "logowanie w kontekście zostanie zablokowane",
            [
                FormField("type", "Rodzaj kontekstu", "select", "Nip", CONTEXT_TYPES, required=True),
                FormField("value", "Identyfikator", value=nip, required=True),
            ],
            lambda v: ("context/block", {"contextIdentifier": {"type": v["type"], "value": v["value"]}}),
        ),
        (
            "Odblokuj kontekst",
            "",
            [
                FormField("type", "Rodzaj kontekstu", "select", "Nip", CONTEXT_TYPES, required=True),
                FormField("value", "Identyfikator", value=nip, required=True),
            ],
            lambda v: ("context/unblock", {"contextIdentifier": {"type": v["type"], "value": v["value"]}}),
        ),
        (
            "Ustaw limity sesji",
            "dla bieżącego kontekstu",
            [
                FormField(
                    "size",
                    "Maks. rozmiar faktury (MB, 0-5)",
                    value="1",
                    required=True,
                    restrict=r"[0-9]*",
                    check=check_range(0, 5),
                ),
                FormField(
                    "attachment",
                    "Z załącznikiem (MB, 0-10)",
                    value="3",
                    required=True,
                    restrict=r"[0-9]*",
                    check=check_range(0, 10),
                ),
                FormField(
                    "count",
                    "Maks. faktur w sesji (1-100 000)",
                    value="10000",
                    required=True,
                    restrict=r"[0-9]*",
                    check=check_range(1, 100_000),
                ),
            ],
            lambda v: (
                "limits/context/session",
                {
                    kind: {
                        "maxInvoiceSizeInMB": int(v["size"]),
                        "maxInvoiceWithAttachmentSizeInMB": int(v["attachment"]),
                        "maxInvoices": int(v["count"]),
                    }
                    for kind in ("onlineSession", "batchSession")
                }
                | {"collectiveIdentifier": {"maxInvoices": 500}},
            ),
        ),
        ("Przywróć domyślne limity sesji", "", [], lambda v: ("limits/context/session", None, "DELETE")),
        ("Ustaw produkcyjne limity API", "jak na produkcji", [], lambda v: ("rate-limits/production", None)),
        ("Przywróć domyślne limity API", "", [], lambda v: ("rate-limits", None, "DELETE")),
        (
            "Ustaw limity certyfikatów",
            "dla bieżącego podmiotu",
            [
                FormField(
                    "enrollments",
                    "Maks. wniosków (0-1000)",
                    value="300",
                    required=True,
                    restrict=r"[0-9]*",
                    check=check_range(0, 1000),
                ),
                FormField(
                    "certificates",
                    "Maks. certyfikatów (0-1000)",
                    value="100",
                    required=True,
                    restrict=r"[0-9]*",
                    check=check_range(0, 1000),
                ),
            ],
            lambda v: (
                "limits/subject/certificate",
                {
                    "enrollment": {"maxEnrollments": int(v["enrollments"])},
                    "certificate": {"maxCertificates": int(v["certificates"])},
                },
            ),
        ),
        ("Przywróć domyślne limity certyfikatów", "", [], lambda v: ("limits/subject/certificate", None, "DELETE")),
    ]


class TestDataScreen(Screen):
    """Operacje /testdata - dostępne wyłącznie na środowisku testowym."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Wróć")]

    app: KsefApp

    def compose(self) -> ComposeResult:
        yield from topbar("[b]Dane testowe KSeF[/b]", self.app)
        yield Static(
            "Tworzenie fikcyjnych podmiotów, osób i uprawnień oraz zmiana limitów. "
            "[$text-warning]Działa tylko na środowisku testowym.[/]",
            classes="screen-description",
        )
        self.operations = _test_operations(self.app.profile.seller.nip)
        options = []
        for n, (title, description, _, _) in enumerate(self.operations):
            prompt = Text.from_markup(f"[b]{escape(title)}[/b]")
            if description:
                prompt.append(f"  {description}", style="dim")
            options.append(Option(prompt, id=str(n)))
        yield OptionList(*options, id="operations")
        yield Footer()

    def on_mount(self) -> None:
        if self.app.profile.env != "test":
            self.notify("Dane testowe są dostępne tylko na środowisku testowym.", severity="error")
            self.app.pop_screen()

    @on(OptionList.OptionSelected, "#operations")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        title, description, fields, build = self.operations[int(event.option.id or 0)]
        consequence = TEST_CONSEQUENCES.get(title)
        if fields:

            async def submit(values: dict[str, Any]) -> str | None:
                return await self._run(title, build(values))

            review = (lambda v: escape(consequence)) if consequence else None
            form = FormScreen(title, fields, intro=description, submit="Wykonaj", on_submit=submit, review=review)
            self.app.push_screen(form)
        elif consequence:
            self.app.push_screen(
                ConfirmScreen(title, escape(consequence), confirm="Wykonaj"),
                lambda ok: ok and self._execute(title, build({})),
            )
        else:
            self._execute(title, build({}))

    @work(exclusive=True)
    async def _execute(self, title: str, spec: tuple) -> None:
        if (problem := await self._run(title, spec)) is not None:
            self.notify(problem, title=title, severity="error", timeout=10)

    async def _run(self, title: str, spec: tuple) -> str | None:
        """Operacja na danych testowych; zwraca opis błędu albo None."""
        if self.app.profile.env != "test":
            return "Dane testowe są dostępne tylko na środowisku testowym KSeF."
        operation, body, *method = spec
        client = await self.app.get_client()
        if client is None:
            return "Nie połączono z KSeF - nic nie zostało wysłane."
        try:
            await client.testdata(operation, body, method=method[0] if method else "POST")
        except KsefError as exc:
            return exc.human()
        self.notify("Gotowe.", title=title, timeout=4)
        return None


# --------------------------------------------------------------------------- menu


def _permissions_screen() -> Screen:
    from .permissions import PermissionsScreen

    return PermissionsScreen()


def _certificates_screen() -> Screen:
    from .certificates import CertificatesScreen

    return CertificatesScreen()


SECTIONS: list[tuple[str, str, Any]] = [
    ("Tokeny KSeF", "Generowanie i unieważnianie tokenów do logowania programów", TokensScreen),
    ("Uprawnienia", "Kto ma dostęp do faktur firmy - nadawanie i odbieranie uprawnień", _permissions_screen),
    ("Certyfikaty KSeF", "Wnioski o certyfikat, pobieranie i unieważnianie certyfikatów", _certificates_screen),
    ("Sesje logowania", "Aktywne logowania do KSeF i ich unieważnianie", AuthSessionsScreen),
    ("Limity", "Limity sesji, certyfikatów i zapytań API", LimitsScreen),
    ("Identyfikatory zbiorcze", "Jedna płatność za wiele faktur", CollectiveScreen),
    ("Dostawcy Peppol", "Lista dostawców usług Peppol w KSeF", PeppolScreen),
    ("Dane testowe", "Fikcyjne podmioty, osoby, uprawnienia i limity (tylko środowisko testowe)", TestDataScreen),
]


class AccountScreen(Screen):
    HELP = (
        "Administracja dostępem do KSeF: tokeny, uprawnienia, certyfikaty, sesje logowania i limity. "
        "Stan wysłanych faktur i UPO jest w Wysyłkach (w na liście faktur)."
    )
    BINDINGS = [Binding("escape", "app.pop_screen", "Wróć"), Binding("p", "profile", "Edytuj profil")]

    app: KsefApp

    def compose(self) -> ComposeResult:
        profile = self.app.profile
        yield from topbar("[b]Konto KSeF[/b]", self.app)
        method = "token KSeF" if profile.auth.metoda == "token" else "certyfikat (XAdES)"
        context = fmt_ident({"type": profile.context_type, "value": profile.context_id})
        status = "połączono" if self.app.client and self.app.client.auth_reference else "jeszcze nie zalogowano"
        yield Static(
            f"[b]{escape(profile.seller.nazwa)}[/b]\n"
            f"[$foreground-muted]Kontekst:[/] {escape(context)}   "
            f"[$foreground-muted]logowanie:[/] {method} ({status})   "
            f"[$foreground-muted]profil:[/] {escape(short_path(profile.path))}",
            classes="screen-description",
        )
        options: list[Option | None] = []
        for n, (title, description, factory) in enumerate(SECTIONS):
            if factory is TestDataScreen and profile.env != "test":
                continue
            prompt = Text.from_markup(f"[b]{escape(title)}[/b]\n")
            prompt.append(description, style="dim")
            options += [Option(prompt, id=str(n)), None]
        prompt = Text.from_markup("[b]Edytuj profil[/b]\n")
        prompt.append("Dane firmy, kontrahenci, pozycje, numeracja i logowanie (profile.toml)", style="dim")
        options.append(Option(prompt, id="profile"))
        yield OptionList(*options, id="sections")
        yield Footer()

    @on(OptionList.OptionSelected, "#sections")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id == "profile":
            self.action_profile()
            return
        self.app.push_screen(SECTIONS[int(event.option.id or 0)][2]())

    def action_profile(self) -> None:
        self.app.edit_profile()

"""Ekran główny: lista faktur wystawionych/otrzymanych z wyszukiwaniem i filtrami."""

from __future__ import annotations

import sqlite3
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.markup import escape
from textual.screen import Screen
from textual.timer import Timer
from textual.validation import Function
from textual.widgets import Button, DataTable, Footer, Input, Label, Select, Static, Tab, Tabs
from textual.widgets.data_table import RowDoesNotExist

from ..client import CredentialsRejected, KsefError, RateLimitError
from ..money import fmt_amount, fmt_date, parse_date
from ..profile import SYNC_SUBJECTS
from ..store import Filters, Found, InvoiceRow, fold
from ..sync import SUBJECTS, backfill, sync
from .common import ChoiceScreen, FormField, FormScreen, fmt_dt, topbar

if TYPE_CHECKING:
    from .app import KsefApp

DIRECTIONS = {
    "issued": "Wystawione",
    "received": "Otrzymane",
    "third": "Jako podmiot 3",
    "authorized": "Jako upoważniony",
    "all": "Wszystkie",
}
DIRECTION_MARKS = {
    "issued": Text("↑", style="bold green"),
    "received": Text("↓", style="bold cyan"),
    "third": Text("3", style="bold magenta"),
    "authorized": Text("U", style="bold yellow"),
    "other": Text("•", style="bold"),
}
AUTO_SYNC_AFTER = timedelta(minutes=15)
SUBJECT_OF = {direction: subject for subject, direction in SUBJECTS.items()}
SYNC_ERRORS = {
    "permissions": "brak uprawnień",
    "rate_limit": "limit zapytań",
    "connection": "brak połączenia",
    "other": "błąd pobierania",
}
DRAFTS = "drafts"  # zakładka ze szkicami (widoczna, gdy jakieś są)
TAB_LABELS = {**DIRECTIONS, DRAFTS: "Szkice", "other": "Inna rola"}
PAGE = 5000  # tyle faktur naraz na liście (sumy i liczby zawsze dla wszystkich pasujących)
DRAFT_STATES = {
    None: Text("szkic", style="dim"),
    "sending": Text("czeka na wynik", style="yellow"),
    "processing": Text("czeka na wynik", style="yellow"),
    "unknown": Text("czeka na wynik", style="yellow"),
    "rejected": Text("odrzucona", style="red"),
    "not_submitted": Text("nie wysłano", style="yellow"),
}


def plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _prev_month_start(d: date) -> date:
    return (_month_start(d) - timedelta(days=1)).replace(day=1)


def period_range(key: str, today: date) -> tuple[date | None, date | None]:
    if key == "this_month":
        return _month_start(today), today
    if key == "prev_month":
        start = _prev_month_start(today)
        return start, _month_start(today) - timedelta(days=1)
    if key == "last_3":
        return _prev_month_start(_prev_month_start(today)), today
    if key == "this_year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    if key == "prev_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    return None, None


PERIODS = [
    ("Ten miesiąc", "this_month"),
    ("Poprzedni miesiąc", "prev_month"),
    ("Ostatnie 3 miesiące", "last_3"),
    ("Ten rok", "this_year"),
    ("Poprzedni rok", "prev_year"),
    ("Wszystko", "all"),
    ("Własny zakres", "custom"),
]


def gross_text(row: InvoiceRow) -> str:
    return f"{fmt_amount(row.gross)} {row.currency}"


def _date_ok(value: str) -> bool:
    return not value.strip() or parse_date(value) is not None


def _ago(moment: datetime | None) -> str:
    if moment is None:
        return "nigdy"
    seconds = (datetime.now(UTC) - moment).total_seconds()
    if seconds < 90:
        return "przed chwilą"
    if seconds < 3600:
        return f"{int(seconds // 60)} min temu"
    if seconds < 86400:
        return f"{int(seconds // 3600)} godz. temu"
    return moment.astimezone().strftime("%d.%m.%Y %H:%M")


class InvoiceTable(DataTable):
    """Lista faktur: ←/→ przełączają zakładki (kolumny i tak mieszczą się w oknie), ↑/↓ wybierają fakturę."""

    BINDINGS = [
        Binding("left", "screen.previous_tab", "Zakładki", key_display="←→"),
        Binding("right", "screen.next_tab", "Zakładki", show=False),
    ]


class MainScreen(Screen):
    HELP = (
        "Lista faktur z lokalnej bazy - wyszukiwanie i filtry działają bez połączenia z KSeF. "
        "Zakładki przełączają faktury wystawione, otrzymane i szkice; Enter otwiera podgląd. "
        "Nowe faktury z KSeF pobiera synchronizacja (r). Wszystkie czynności są też w menu Akcje (a)."
    )
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (110, "-wide")]
    BINDINGS = [
        Binding("n", "new_invoice", "Nowa faktura", priority=False),
        Binding("a", "menu", "Akcje"),
        Binding("r", "sync", "Synchronizuj"),
        Binding("slash", "focus_search", "Szukaj", key_display="/"),
        Binding("w", "sent", "Wysyłki"),
        Binding("f", "clear_filters", "Wyczyść filtry", show=False),
        Binding("i", "import", "Wyślij XML"),
        Binding("g", "fetch", "Pobierz po nr KSeF", show=False),
        Binding("m", "more", "Wczytaj więcej", show=False),
        Binding("k", "account", "Konto KSeF"),
        Binding("1", "direction(1)", "Zakładka o tym numerze", show=False),
        Binding("2", "direction(2)", "Zakładka o tym numerze", show=False),
        Binding("3", "direction(3)", "Zakładka o tym numerze", show=False),
        Binding("4", "direction(4)", "Zakładka o tym numerze", show=False),
        Binding("5", "direction(5)", "Zakładka o tym numerze", show=False),
        # Działają też, gdy aktywne jest pole wyszukiwania lub filtr (w polu tekstowym ←/→ zostają dla kursora).
        Binding("left", "previous_tab", "Poprzednia zakładka", show=False),
        Binding("right", "next_tab", "Następna zakładka", show=False),
        Binding("up", "move_selection(-1)", "Poprzednia faktura", show=False),
        Binding("down", "move_selection(1)", "Następna faktura", show=False),
        Binding("delete", "discard_draft", "Porzuć szkic", key_display="Del"),
        Binding("escape", "focus_table", "Wróć do listy"),
        Binding("q", "app.quit", "Wyjście"),
    ]

    app: KsefApp

    def __init__(self) -> None:
        super().__init__()
        self.direction = self.app.profile.directions[0]
        self._search_timer: Timer | None = None
        self._syncing = False
        self._rows: list[InvoiceRow] = []
        self._drafts: list = []
        self._found = Found([])
        self._limit = PAGE  # „m - wczytaj więcej” zwiększa limit o kolejną porcję
        self._stale = False  # filtry są w trakcie zmiany - lista pokazuje poprzednie wyniki

    # ---------------------------------------------------------------- układ

    def compose(self) -> ComposeResult:
        profile = self.app.profile
        # NIP przed nazwą: w wąskim oknie skraca się koniec nazwy, a nie identyfikator firmy.
        title = f"[b]NIP {profile.seller.nip}[/b]  {escape(profile.seller.nazwa)}"
        if profile.context_id != profile.seller.nip:
            title += f"  [$foreground-muted]kontekst {escape(profile.context_id)}[/]"
        yield from topbar(title, self.app)
        tabs = [*profile.directions, "all"] if len(profile.directions) > 1 else profile.directions
        drafts = [Tab("Szkice", id=DRAFTS)] if self.app.store.drafts() else []
        yield Tabs(*(Tab(DIRECTIONS[d], id=d) for d in tabs), *drafts, id="direction-tabs")
        with Vertical(id="filters"):
            with Horizontal(id="search-row"):
                yield Input(
                    placeholder="Szukaj: numer faktury, kontrahent, NIP, nazwa usługi, numer KSeF…  ( / )",
                    id="search",
                    compact=True,
                )
                yield Button("☰ Akcje", id="actions", compact=True, tooltip="Wszystkie czynności (a)")
            with Horizontal(id="filter-row"):
                yield Label("Data wystawienia", classes="filter-label first", id="period-label")
                yield Select(PERIODS, value="this_year", allow_blank=False, id="period", compact=True)
                yield Label("od", classes="filter-label")
                # Błąd daty pokazujemy po wpisaniu (wyjście z pola, Enter), a nie przy każdym znaku.
                validate = {"blur", "submitted"}
                yield Input(
                    placeholder="DD.MM.RRRR",
                    id="date-from",
                    validators=[Function(_date_ok)],
                    validate_on=validate,
                    compact=True,
                )
                yield Label("do", classes="filter-label")
                yield Input(
                    placeholder="DD.MM.RRRR",
                    id="date-to",
                    validators=[Function(_date_ok)],
                    validate_on=validate,
                    compact=True,
                )
                yield Label("Kontrahent", classes="filter-label", id="counterparty-label")
                yield Select([], prompt="Wszyscy kontrahenci", id="counterparty", compact=True)
            yield Static("", id="filter-error")
        yield InvoiceTable(cursor_type="row", zebra_stripes=True, id="invoices")
        yield Static("", id="empty")
        with Horizontal(id="status-bar"):
            yield Static("", id="status")
            yield Static("", id="sync-status")
        yield Footer()

    def on_mount(self) -> None:
        self.watch(self.app, "connection", lambda _: self._update_status(), init=False)
        self._show_list()
        last = self.app.store.last_sync()
        # Automatycznie tylko z połączeniem - w trybie lokalnym nie pytamy o logowanie bez prośby użytkownika.
        stale = last is None or datetime.now(UTC) - last > AUTO_SYNC_AFTER
        if self.app.client is not None and stale and not self._sync_blocked():
            self.action_sync()

    def _show_list(self) -> None:
        self._apply_period("this_year")
        self.refresh_counterparties()
        self.reload()
        self.query_one("#invoices", DataTable).focus()

    def rebuild(self) -> None:
        """Po zmianie profilu w tej samej firmie (np. rodzaje faktur, nazwa) - układ od nowa."""
        profile = self.app.profile
        if self.direction not in (*profile.directions, "all", DRAFTS):
            self.direction = profile.directions[0]
        self.refresh(recompose=True)
        self.call_after_refresh(self._show_list)

    def _columns(self, rows: list[InvoiceRow]) -> tuple[list[str], dict[str, int]]:
        """Dobiera kolumny do szerokości terminala: gdy brakuje miejsca, chowa mniej ważne.

        Waluta jest częścią kwoty brutto - nigdy nie znika (EUR nie może wyglądać jak PLN).
        """
        amount = max(9, *(len(fmt_amount(r.net)) for r in rows)) if rows else 9
        gross = max(13, *(len(gross_text(r)) for r in rows)) if rows else 13
        widths = {
            "dir": 1,
            "date": 10,
            "number": max(13, *(len(r.invoice_number) for r in rows)) if rows else 13,
            "nip": min(16, max(10, *(len(r.counterparty_id) for r in rows))) if rows else 10,
            "net": amount,
            "vat": amount,
            "gross": gross,
        }
        columns = ["date", "number", "party", "nip", "net", "vat", "gross"]
        if self.direction == "all":
            columns.insert(0, "dir")
        # Każda kolumna ma po 1 znaku odstępu z obu stron; do tego marginesy i pasek przewijania.
        available = self.size.width - 6
        fixed = lambda: sum(widths[c] + 2 for c in columns if c != "party")  # noqa: E731
        for optional in ("nip", "vat", "net"):
            if available - fixed() - 2 >= 18:
                break
            columns.remove(optional)
        widths["party"] = max(12, available - fixed() - 2)
        return columns, widths

    def _setup_columns(self, columns: list[str], widths: dict[str, int]) -> None:
        table = self.query_one("#invoices", DataTable)
        table.clear(columns=True)
        labels = {
            "dir": "",
            "date": "Data wyst.",
            "number": "Numer faktury",
            "party": {"issued": "Nabywca", "all": "Kontrahent"}.get(self.direction, "Sprzedawca"),
            "nip": "NIP",
            "net": Text("Netto", justify="right"),
            "vat": Text("VAT", justify="right"),
            "gross": Text("Brutto", justify="right"),
        }
        for column in columns:
            table.add_column(labels[column], key=column, width=widths[column])

    # ------------------------------------------------------------- dane

    def current_filters(self) -> Filters:
        counterparty = self.query_one("#counterparty", Select).value
        return Filters(
            direction=None if self.direction in ("all", DRAFTS) else self.direction,
            date_from=parse_date(self.query_one("#date-from", Input).value),
            date_to=parse_date(self.query_one("#date-to", Input).value),
            counterparty=None if counterparty is Select.NULL else str(counterparty),
            text=self.query_one("#search", Input).value,
        )

    def _sync_drafts_tab(self) -> None:
        """Zakładka „Szkice” jest tylko wtedy, gdy są otwarte szkice."""
        tabs = self.query_one("#direction-tabs", Tabs)
        has_tab = any(tab.id == DRAFTS for tab in tabs.query(Tab))
        has_drafts = bool(self.app.store.drafts())
        if has_drafts and not has_tab:
            tabs.add_tab(Tab("Szkice", id=DRAFTS))
        elif has_tab and not has_drafts:
            if self.direction == DRAFTS:
                self.direction = self.app.profile.directions[0]
                tabs.active = self.direction
            tabs.remove_tab(DRAFTS)

    def reload(self, select: str | None = None) -> None:
        """Odświeża listę; kursor zostaje na tej samej fakturze (albo przechodzi na `select`)."""
        self._sync_drafts_tab()
        self.query_one("#filter-row").display = self.direction != DRAFTS
        if self.direction == DRAFTS:
            self._reload_drafts(select)
            return
        table = self.query_one("#invoices", DataTable)
        problem = self._date_problem(complete_only=True)
        if problem:
            self._filter_error(problem)
        if problem or self._typing_date():
            self._stale = True  # nie gubimy listy, dopóki daty nie są kompletne i poprawne
            self._update_status()
            return
        self._filter_error("")
        filters = self.current_filters()
        found = self.app.store.find_invoices(filters, self._limit)
        if found.error == "syntax":  # niedokończone zapytanie - zostają poprzednie wyniki
            self._stale = True
            self._update_status()
            return
        if found.error:
            self._filter_error(f"Nie udało się przeszukać bazy: {found.error}")
        self._stale = False
        self._found = found
        rows = found.rows
        self._rows = rows
        selected_key = select
        if selected_key is None and table.row_count and table.cursor_row < table.row_count:
            selected_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        columns, widths = self._columns(rows)
        self._setup_columns(columns, widths)
        for row in rows:
            name = row.counterparty_name
            if len(name) > widths["party"]:
                name = name[: widths["party"] - 1] + "…"
            cells = {
                "dir": DIRECTION_MARKS.get(row.direction, Text("?")),
                "date": fmt_date(row.issue_date),
                "number": row.invoice_number,
                "party": name,
                "nip": row.counterparty_id,
                "net": Text(fmt_amount(row.net), justify="right"),
                "vat": Text(fmt_amount(row.vat), justify="right"),
                "gross": Text(gross_text(row), justify="right", style="bold"),
            }
            table.add_row(*(cells[c] for c in columns), key=f"{row.direction}|{row.ksef_number}")
        if selected_key:
            with suppress(RowDoesNotExist):
                table.move_cursor(row=table.get_row_index(selected_key))
        self._update_empty(filters)
        self._update_status()

    def _reload_drafts(self, select: str | None = None) -> None:
        table = self.query_one("#invoices", DataTable)
        selected_key = select
        if selected_key is None and table.row_count and table.cursor_row < table.row_count:
            selected_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        text = fold(self.query_one("#search", Input).value.strip())
        drafts = [d for d in self.app.store.drafts() if not text or text in fold(f"{d['number']} {d['buyer_name']}")]
        self._drafts = drafts
        self._rows = []
        amount = max([len(f"{fmt_amount(d['gross'] or 0)} {d['currency']}") for d in drafts] + [11])
        narrow = self.size.width < 110
        number = max([len(d["number"] or "") for d in drafts] + [10])
        widths = {"updated": 11 if narrow else 16, "number": number, "gross": amount, "state": 14}
        party = max(12, self.size.width - 6 - sum(w + 2 for w in widths.values()) - 2)
        table.clear(columns=True)
        table.add_column("Zmieniony", key="updated", width=widths["updated"])
        table.add_column("Numer", key="number", width=widths["number"])
        table.add_column("Nabywca", key="party", width=party)
        table.add_column(Text("Brutto", justify="right"), key="gross", width=widths["gross"])
        table.add_column("Stan", key="state", width=widths["state"])
        for d in drafts:
            name = d["buyer_name"] or "(bez nabywcy)"
            if len(name) > party:
                name = name[: party - 1] + "…"
            updated = fmt_dt(d["updated"])
            table.add_row(
                updated[:5] + updated[10:] if narrow else updated,  # „24.10 19:12” w wąskim oknie
                d["number"] or "",
                name,
                Text(f"{fmt_amount(d['gross'] or 0)} {d['currency'] or 'PLN'}", justify="right", style="bold"),
                DRAFT_STATES.get(d["send_status"], DRAFT_STATES[None]),
                key=f"{DRAFTS}|{d['id']}",
            )
        if selected_key:
            with suppress(RowDoesNotExist):
                table.move_cursor(row=table.get_row_index(selected_key))
        empty = self.query_one("#empty", Static)
        table.display = bool(drafts)
        empty.display = not drafts
        empty.update("[b]Brak szkiców pasujących do wyszukiwania[/b]")
        n = len(drafts)
        self.query_one("#status", Static).update(
            f"[b]{n}[/b] {plural(n, 'szkic', 'szkice', 'szkiców')} · nie wysłano do KSeF"
            "    [$foreground-muted]Enter - otwórz, Del - porzuć[/]"
        )

    def _update_empty(self, filters: Filters) -> None:
        """Pusta lista odpowiada na pytanie: nic nie pobrano, nic nie ma, czy filtry wszystko ukryły?"""
        empty = self.query_one("#empty", Static)
        table = self.query_one("#invoices", DataTable)
        if self._rows:
            empty.display = False
            table.display = True
            return
        table.display = False
        empty.display = True
        store = self.app.store
        company = escape(self.app.profile.seller.nazwa)
        if store.count() == 0:
            if self._syncing:
                empty.update(
                    "[b]Pobieram faktury z KSeF…[/b]\n\n"
                    "[$foreground-muted]Pierwsza synchronizacja może potrwać chwilę.[/]"
                )
            elif store.last_sync() is None:
                empty.update(
                    f"[b]Nie pobrano jeszcze faktur dla {company}[/b]\n\n"
                    "Naciśnij [b $accent]r[/], aby pobrać faktury z KSeF,\n"
                    "[b $accent]n[/], aby wystawić nową fakturę,\n"
                    "albo [b $accent]i[/], aby wysłać gotowe pliki XML."
                )
            else:
                since = fmt_date(self.app.profile.sync_since)
                empty.update(
                    f"[b]W KSeF nie ma faktur {company} od {since}[/b]\n\n"
                    "Naciśnij [b $accent]n[/], aby wystawić pierwszą fakturę.\n"
                    "[$foreground-muted]Wcześniejsze faktury pobierzesz z menu Akcje (a) → Pobierz starsze faktury.[/]"
                )
            return
        everything = store.find_invoices(Filters(direction=filters.direction), limit=0).total
        tab = TAB_LABELS.get(self.direction, "").lower()
        errors = self._sync_errors()
        if everything:
            empty.update(
                "[b]Brak wyników dla tych filtrów[/b]\n\n"
                f"W zakładce {escape(tab)} jest {everything} {self._plural(everything)} - "
                "naciśnij [b $accent]f[/], aby wyczyścić filtry."
            )
        elif errors:
            reasons = "\n".join(f"{escape(DIRECTIONS[d])}: {escape(a['error'])}" for d, a in errors)
            empty.update(
                f"[b]Nie udało się pobrać faktur w zakładce {escape(tab)}[/b]\n\n{reasons}\n\n"
                "[$foreground-muted]Naciśnij [b $accent]r[/], aby spróbować ponownie.[/]"
            )
        else:
            empty.update(f"[b]Brak faktur w zakładce {escape(tab)}[/b]")

    def _update_status(self, sync_message: str | None = None) -> None:
        if self.direction == DRAFTS:
            return  # pasek stanu szkiców ustawia _reload_drafts
        found = self._found
        count = found.unique if self.direction == "all" else found.total
        parts = [f"[b]{count}[/b] {self._plural(count)}"]
        net, vat, gross = found.pln
        if count and found.foreign < found.unique:
            # Brutto pierwsze - w wąskim oknie koniec paska się ucina.
            parts.append(
                f"brutto [b]{fmt_amount(gross)}[/b] PLN · netto [b]{fmt_amount(net)}[/b] · VAT [b]{fmt_amount(vat)}[/b]"
            )
        if found.foreign:
            parts.append(f"[$foreground-muted]+{found.foreign} w innych walutach (poza sumą)[/]")
        if self.direction == "all" and found.unique < found.total:
            parts.append("[$foreground-muted]faktura w dwóch rolach liczona raz[/]")
        if found.truncated:
            parts.append(f"[$text-warning]pokazano {len(self._rows)} z {found.total} (m - więcej)[/]")
        text = self.query_one("#search", Input).value.strip()
        if text and not self._stale:
            missing = self.app.store.find_invoices(replace(self.current_filters(), text=""), limit=0).without_xml
            if missing:
                parts.append(f"[$foreground-muted]{missing} bez pobranej treści - szukane tylko po metadanych[/]")
        if self._stale:
            parts.append("[$text-warning](poprzednie wyniki)[/]")
        self.query_one("#status", Static).update(" · ".join(parts))
        waiting = len(self.app.store.unresolved_sent())
        if waiting and not sync_message:
            # Bez tej podpowiedzi wysyłka bez wyniku „znika” - a nie wolno jej wysyłać drugi raz.
            waits = plural(waiting, "wysyłka czeka", "wysyłki czekają", "wysyłek czeka")
            right = f"[$text-warning]{waiting} {waits} na wynik (w)[/]"
        elif sync_message:
            right = f"[$text-accent]⟳ {escape(sync_message)}[/]"
        else:
            right = self._sync_text()
        self.query_one("#sync-status", Static).update(right)

    def _tab_directions(self) -> list[str]:
        profile = self.app.profile
        return list(profile.directions) if self.direction == "all" else [self.direction]

    def _sync_errors(self) -> list[tuple[str, sqlite3.Row]]:
        """Rodzaje faktur z bieżącej zakładki, których ostatnia synchronizacja się nie udała."""
        store = self.app.store
        errors = []
        for direction in self._tab_directions():
            attempt = store.sync_attempt(SUBJECT_OF.get(direction, ""))
            if attempt is not None and attempt["error"]:
                errors.append((direction, attempt))
        return errors

    def _sync_text(self) -> str:
        """Stan pobierania dla bieżącej zakładki: kiedy pobrano, czego się nie udało, kiedy kolejna próba."""
        store = self.app.store
        times = [store.last_sync(SUBJECT_OF.get(d, "")) for d in self._tab_directions()]
        known = [t for t in times if t is not None]
        last = min(known) if known and len(known) == len(times) else None  # zakładka „Wszystkie” - najstarsze
        parts = [f"pobrano {_ago(last)}"]
        if self.app.client is None:
            parts.insert(0, "tryb lokalny")
        text = f"[$foreground-muted]{' · '.join(parts)}[/]"
        problems = [
            f"{DIRECTIONS[d].lower()}: {SYNC_ERRORS[a['error_kind'] or 'other']}" for d, a in self._sync_errors()
        ]
        if blocked := self._sync_blocked():
            problems.append(f"kolejna próba od {blocked.astimezone():%H:%M}")
        if problems:  # „pobrano przed chwilą” przy nieudanej próbie by myliło - zostaje sam problem
            return f"[$text-warning]{escape(' · '.join(problems))}[/]"
        return text

    def _sync_blocked(self) -> datetime | None:
        """Do kiedy KSeF prosi o przerwę w zapytaniach (Retry-After) - None, gdy można pobierać."""
        until = self.app.sync_blocked_until
        return until if until and until > datetime.now(UTC) else None

    @staticmethod
    def _plural(n: int) -> str:
        if n == 1:
            return "faktura"
        if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
            return "faktury"
        return "faktur"

    def refresh_counterparties(self) -> None:
        select = self.query_one("#counterparty", Select)
        current = select.value
        if self.direction == DRAFTS:
            return
        direction = None if self.direction == "all" else self.direction
        options = [(f"{name[:38]}  ({nip})", nip) for nip, name, _count in self.app.store.counterparties(direction)]
        with self.prevent(Select.Changed):
            select.set_options(options)
            if current is not Select.NULL and any(value == current for _, value in options):
                select.value = current

    def _apply_period(self, key: str) -> None:
        start, end = period_range(key, date.today())
        if key == "custom":
            return
        with self.prevent(Input.Changed):
            self.query_one("#date-from", Input).value = fmt_date(start)
            self.query_one("#date-to", Input).value = fmt_date(end)

    # --------------------------------------------------------- zdarzenia

    @on(Tabs.TabActivated, "#direction-tabs")
    def _direction_changed(self, event: Tabs.TabActivated) -> None:
        if event.tab.id and event.tab.id != self.direction:
            self.direction = event.tab.id
            self.refresh_counterparties()
            self.reload()
            self.refresh_bindings()

    @on(Input.Changed, "#search")
    def _search_changed(self) -> None:
        if self._search_timer:
            self._search_timer.stop()
        self._search_timer = self.set_timer(0.15, self.reload)

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self.action_focus_table()

    @on(Input.Changed, "#date-from")
    @on(Input.Changed, "#date-to")
    def _dates_changed(self, event: Input.Changed) -> None:
        period = self.query_one("#period", Select)
        if period.value != "custom":
            with self.prevent(Select.Changed):
                period.value = "custom"
        self.reload()

    @on(Input.Blurred, "#date-from")
    @on(Input.Blurred, "#date-to")
    @on(Input.Submitted, "#date-from")
    @on(Input.Submitted, "#date-to")
    def _date_done(self) -> None:
        if problem := self._date_problem():
            self._filter_error(problem)
            self._update_status()

    def _typing_date(self) -> bool:
        """Data w trakcie wpisywania (np. „01.1”) - jeszcze nie błąd, ale też nie filtr."""
        return any(
            value.strip() and parse_date(value) is None
            for value in (self.query_one(f"#{i}", Input).value for i in ("date-from", "date-to"))
        )

    def _date_problem(self, *, complete_only: bool = False) -> str:
        """Opis problemu z zakresem dat albo "" (complete_only - tylko gdy obie daty są już wpisane w całości)."""
        values = {key: self.query_one(f"#date-{key}", Input).value.strip() for key in ("from", "to")}
        dates = {key: parse_date(value) for key, value in values.items()}
        if not complete_only:
            for key, label in (("from", "od"), ("to", "do")):
                if values[key] and dates[key] is None:
                    return f"Data „{label}” w formacie DD.MM.RRRR, np. 01.10.2026."
        if dates["from"] and dates["to"] and dates["from"] > dates["to"]:
            return "Data „od” nie może być późniejsza niż data „do”."
        return ""

    def _filter_error(self, text: str) -> None:
        error = self.query_one("#filter-error", Static)
        error.update(f"[$text-error]{escape(text)}[/]" if text else "")
        error.display = bool(text)

    @on(Select.Changed, "#period")
    def _period_changed(self, event: Select.Changed) -> None:
        self._apply_period(str(event.value))
        self.reload()

    @on(Select.Changed, "#counterparty")
    def _counterparty_changed(self) -> None:
        self.reload()

    @on(DataTable.RowSelected, "#invoices")
    def _open_invoice(self, event: DataTable.RowSelected) -> None:
        direction, key = str(event.row_key.value).split("|", 1)
        if direction == DRAFTS:
            self.app.open_draft(int(key))
        else:
            self.app.open_invoice(key, direction)

    def action_discard_draft(self) -> None:
        table = self.query_one("#invoices", DataTable)
        if self.direction != DRAFTS or not table.row_count:
            return
        key = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        self.app.discard_draft(int(key.split("|", 1)[1]))

    def on_resize(self) -> None:
        if self.is_mounted:
            self.reload()

    def shows(self, ksef_number: str, direction: str) -> bool:
        """Czy faktura jest widoczna na liście przy obecnej zakładce i filtrach."""
        try:
            self.query_one("#invoices", DataTable).get_row_index(f"{direction}|{ksef_number}")
        except RowDoesNotExist:
            return False
        return True

    def action_more(self) -> None:
        """Wczytaj więcej: lista pokazuje kolejną porcję najstarszych pasujących faktur."""
        if self._found.truncated:
            self._limit += PAGE
            self.reload()

    # ----------------------------------------------------------- akcje

    def action_previous_tab(self) -> None:
        self.query_one("#direction-tabs", Tabs).action_previous_tab()

    def action_next_tab(self) -> None:
        self.query_one("#direction-tabs", Tabs).action_next_tab()

    def action_move_selection(self, step: int) -> None:
        table = self.query_one("#invoices", DataTable)
        if not table.row_count:
            return
        table.focus()
        if step < 0:
            table.action_cursor_up()
        else:
            table.action_cursor_down()

    def action_direction(self, position: int) -> None:
        tabs = self.query_one("#direction-tabs", Tabs)
        ids = [tab.id for tab in tabs.query(Tab)]
        if 0 < position <= len(ids):
            tabs.active = ids[position - 1] or tabs.active

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_focus_table(self) -> None:
        self.query_one("#invoices", DataTable).focus()

    def action_clear_filters(self) -> None:
        self._limit = PAGE
        with self.prevent(Input.Changed, Select.Changed):
            self.query_one("#search", Input).value = ""
            self.query_one("#counterparty", Select).clear()
            self.query_one("#period", Select).value = "all"
        self._apply_period("all")
        self.reload()
        self.notify("Pokazuję wszystkie faktury.", timeout=2)

    def action_new_invoice(self) -> None:
        self.app.start_new_invoice()

    def action_import(self) -> None:
        from .import_screen import ImportScreen

        self.app.push_screen(ImportScreen())

    def action_fetch(self) -> None:
        from .import_screen import fetch_invoice_dialog

        fetch_invoice_dialog(self.app)

    def action_account(self) -> None:
        from .account import AccountScreen

        self.app.push_screen(AccountScreen())

    def action_sent(self) -> None:
        self.app.open_sent()

    @on(Button.Pressed, "#actions")
    def action_menu(self) -> None:
        """Wszystkie czynności w jednym miejscu - z klawiszami, które je skracają."""
        drafts = len(self.app.store.drafts())
        waiting = len(self.app.store.unresolved_sent())
        choices = [
            ("Nowa faktura  (n)", "Wystaw fakturę i wyślij ją do KSeF", self.action_new_invoice),
            ("Synchronizuj  (r)", "Pobierz nowe faktury z KSeF", self.action_sync),
            (
                "Pobierz starsze faktury",
                f"Sprzed {fmt_date(self.app.profile.sync_since)} - zwykła synchronizacja ich nie pobiera",
                self.action_backfill,
            ),
            ("Pobierz fakturę po numerze KSeF  (g)", "Dowolna faktura, do której masz dostęp", self.action_fetch),
            ("Wyślij pliki XML  (i)", "Gotowe faktury FA(3) z dysku", self.action_import),
        ]
        if drafts:
            choices.append((f"Szkice: {drafts}", "Niedokończone faktury zapisane w tej aplikacji", self._show_drafts))
        sent = "Stan wysłanych faktur i potwierdzenia odbioru (UPO)"
        if waiting:
            sent = f"Czeka na wynik z KSeF: {waiting}. " + sent
        choices += [
            ("Wysyłki i UPO  (w)", sent, self.action_sent),
            ("Konto KSeF  (k)", "Tokeny, uprawnienia, certyfikaty, limity", self.action_account),
            ("Pomoc  (F1)", "Opis tego ekranu i wszystkie skróty klawiszowe", self.app.action_help),
        ]
        self.app.push_screen(ChoiceScreen("Akcje", choices), lambda run: run and run())

    def _show_drafts(self) -> None:
        self.query_one("#direction-tabs", Tabs).active = DRAFTS

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        typing = isinstance(self.focused, Input)
        if action == "focus_table":
            return typing
        if action == "discard_draft":
            return self.direction == DRAFTS and not typing
        # Pojedyncze litery nie mogą odpalać akcji, gdy użytkownik pisze w polu tekstowym.
        if action in {
            "new_invoice",
            "sync",
            "clear_filters",
            "direction",
            "import",
            "fetch",
            "account",
            "more",
            "menu",
            "sent",
        }:
            return not typing
        return True

    def action_sync(self) -> None:
        # Bez anulowania trwającej synchronizacji (np. automatycznej po starcie) - druga prośba jest pomijana.
        if self._syncing:
            self.notify("Synchronizacja już trwa…", timeout=2)
            return
        if blocked := self._sync_blocked():
            self.notify(
                f"KSeF ogranicza liczbę zapytań - kolejna próba możliwa od {blocked.astimezone():%H:%M:%S}.",
                severity="warning",
                timeout=5,
            )
            return
        self._syncing = True
        self._sync()

    def action_backfill(self) -> None:
        """Faktury sprzed `synchronizuj_od` - zwykła synchronizacja ich nie pobiera."""
        since = self.app.profile.sync_since

        def check(values: dict) -> tuple[str, str] | None:
            if values["date_to"] < values["date_from"]:
                return "date_to", "data „do” nie może być wcześniejsza niż data „od”"
            return None

        def chosen(values: dict | None) -> None:
            if not values:
                return
            if self._syncing or self._sync_blocked():
                self.action_sync()  # ten sam komunikat: trwa synchronizacja albo przerwa od KSeF
                return
            self._syncing = True
            self._sync((values["date_from"], values["date_to"]))

        self.app.push_screen(
            FormScreen(
                "Pobierz starsze faktury",
                [
                    FormField("date_from", "Od", "date", date(since.year - 1, 1, 1), required=True),
                    FormField("date_to", "Do", "date", since - timedelta(days=1), required=True),
                ],
                intro=f"Synchronizacja pobiera faktury od {fmt_date(since)} (synchronizuj_od w profilu). "
                "Tutaj pobierzesz wcześniejsze, np. z poprzedniego roku - nie zmienia to zwykłej synchronizacji.",
                submit="Pobierz",
                validate=check,
            ),
            chosen,
        )

    @work(group="sync")
    async def _sync(self, older: tuple[date, date] | None = None) -> None:
        try:
            client = await self.app.get_client()
        except BaseException:
            self._syncing = False
            raise
        if client is None:
            self._syncing = False
            return
        self._update_empty(self.current_filters())

        def progress(_step: str, state: str) -> None:
            self._update_status(state)

        profile = self.app.profile
        try:
            subjects = [SYNC_SUBJECTS[key][0] for key in profile.sync]
            if older:
                result = await backfill(client, self.app.store, *older, progress, subjects)
            else:
                result = await sync(client, self.app.store, profile.sync_since, progress, subjects)
        except RateLimitError as exc:
            if exc.retry_after:
                self.app.sync_blocked_until = datetime.now(UTC) + timedelta(seconds=exc.retry_after)
            self.notify(exc.human(), title="Limit zapytań KSeF", severity="warning", timeout=10)
        except CredentialsRejected as exc:
            fix = "Ctrl+P → „Zmień token KSeF”" if profile.auth.metoda == "token" else "sprawdź certyfikat w profilu"
            self.notify(
                f"{exc.human()}\nFaktury z lokalnej bazy są dostępne. Aby się połączyć: {fix}.",
                title="KSeF odrzucił dane logowania",
                severity="error",
                timeout=15,
            )
        except KsefError as exc:
            self.notify(exc.human(), title="Synchronizacja nieudana", severity="error", timeout=12)
        else:
            title = "Pobrano starsze faktury" if older else "Synchronizacja zakończona"
            if result.total_added:
                parts = [f"{DIRECTIONS[d].lower()}: {n}" for d, n in result.added.items() if n]
                self.notify("Nowe faktury — " + ", ".join(parts), title=title)
            else:
                self.notify("Brak nowych faktur.", title=title, timeout=3)
        finally:
            self._syncing = False
            # Przy wyjściu z aplikacji w trakcie synchronizacji ekranu już nie ma - nie ma czego odświeżać.
            if self.is_attached:
                with suppress(NoMatches):
                    self.refresh_counterparties()
                    self.reload()

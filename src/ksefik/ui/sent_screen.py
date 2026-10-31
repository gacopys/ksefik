"""Wysyłki i UPO: historia wysyłek z tej aplikacji (faktury i pliki XML) - działa bez połączenia z KSeF."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from ..client import KsefError
from ..store import SENT_STATES, UNRESOLVED_STATES
from ..submissions import fetch_upo
from .common import fmt_dt, safe_name, topbar
from .invoice_screens import upo_unavailable

if TYPE_CHECKING:
    from .app import KsefApp

STATE_STYLES = {"accepted": "green", "rejected": "red", "not_submitted": "dim"}
SHORT_STATES = {"accepted": "przyjęta", "rejected": "odrzucona", "not_submitted": "nie wysłano"}
# Kolumny (klucz, nagłówek, szerokość). Numer KSeF i źródło pokazujemy, gdy mieszczą się w oknie
# - zawsze są też w szczegółach pod listą.
COLUMNS = [
    ("created", "Wysłano", 16),
    ("number", "Faktura", 18),
    ("state", "Stan", 14),
    ("upo", "UPO", 7),
    ("ksef", "Numer KSeF", 36),
    ("source", "Źródło", 16),
]
OPTIONAL = ("ksef", "source")


class SentScreen(Screen):
    """Każda próba wysłania faktury z jej stanem. Wysyłki bez wyniku sprawdza się tu w KSeF (s)."""

    HELP = (
        "Historia wysyłek z tej aplikacji: faktury wystawione tutaj i wysłane pliki XML. Lista działa bez "
        "połączenia. Wysyłka „czeka na wynik”, gdy KSeF jeszcze jej nie potwierdził - nie wysyłaj wtedy faktury "
        "ponownie, tylko sprawdź status. Enter otwiera przyjętą fakturę albo szkic, z którego wysłano odrzuconą."
    )
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Wróć"),
        Binding("s", "check", "Sprawdź status"),
        Binding("u", "upo", "Pobierz UPO"),
        Binding("e", "sessions", "Sesje w KSeF"),
    ]

    app: KsefApp

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[sqlite3.Row] = []
        self.columns: list[str] = []

    def compose(self) -> ComposeResult:
        yield from topbar("[b]Wysyłki i UPO[/b]", self.app)
        yield Static(
            "Faktury wysłane z tej aplikacji i ich stan w KSeF. Działa bez połączenia; "
            "[b]s[/b] sprawdza w KSeF wysyłki bez wyniku, [b]u[/b] pobiera UPO.",
            classes="screen-description",
        )
        yield DataTable(cursor_type="row", zebra_stripes=True, id="list")
        yield Static("", id="list-empty")
        yield Static("", id="list-details")
        yield Static("", id="op-status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#op-status").display = False
        self.reload()

    def on_resize(self) -> None:
        if self._fit_columns(self.size.width) != self.columns:
            self.reload()

    @staticmethod
    def _fit_columns(width: int) -> list[str]:
        keys = [key for key, _, _ in COLUMNS if key not in OPTIONAL]
        used = sum(w + 2 for key, _, w in COLUMNS if key in keys) + 2  # odstępy komórek i pasek przewijania
        for key, _, w in COLUMNS:
            if key in OPTIONAL and used + w + 2 <= width:
                keys.append(key)
                used += w + 2
        return [key for key, _, _ in COLUMNS if key in keys]

    def reload(self) -> None:
        table = self.query_one("#list", DataTable)
        selected = self.selected["id"] if self.selected else None
        self.rows = self.app.store.sent_history()
        self.columns = self._fit_columns(self.size.width or self.app.size.width)
        table.clear(columns=True)
        for key, label, width in COLUMNS:
            if key in self.columns:
                table.add_column(label, width=width, key=key)
        for row in self.rows:
            cells = self._cells(row)
            table.add_row(*(cells[key] for key in self.columns), key=str(row["id"]))
        empty = self.query_one("#list-empty", Static)
        empty.display = not self.rows
        table.display = self.query_one("#list-details").display = bool(self.rows)
        if not self.rows:
            empty.update(
                "[b]Nie wysłano jeszcze żadnej faktury z tej aplikacji.[/b]\n\n"
                "[$foreground-muted]Wystawione faktury (n na liście faktur) i wysłane pliki XML (i) pojawią się "
                "tutaj razem z ich stanem w KSeF. Wysyłki z innych programów znajdziesz w Sesjach w KSeF (e).[/]"
            )
        else:
            if selected is not None:
                index = next((n for n, r in enumerate(self.rows) if r["id"] == selected), 0)
                table.move_cursor(row=index)
            table.focus()
            self._details()
        self.refresh_bindings()

    @staticmethod
    def _cells(row: sqlite3.Row) -> dict[str, object]:
        status = row["status"]
        state = Text(SHORT_STATES.get(status, "czeka na wynik"), style=STATE_STYLES.get(status, "yellow"))
        if status == "accepted":
            upo = Text("✓", style="green") if row["has_upo"] else Text("później", style="dim")
        else:
            upo = Text("")
        return {
            "created": fmt_dt(row["created"]),
            "number": row["invoice_number"],
            "state": state,
            "upo": upo,
            "ksef": row["ksef_number"] or "",
            "source": _source(row),
        }

    @property
    def selected(self) -> sqlite3.Row | None:
        table = self.query_one("#list", DataTable)
        if not self.rows or table.cursor_row >= len(self.rows):
            return None
        return self.rows[table.cursor_row]

    @on(DataTable.RowHighlighted, "#list")
    def _details(self) -> None:
        row = self.selected
        self.refresh_bindings()
        if row is None:
            return
        lines = [
            f"[$foreground-muted]Stan:[/] [b]{escape(SENT_STATES.get(row['status'], row['status']))}[/b]"
            + (f"   [$foreground-muted]sprawdzono:[/] {fmt_dt(row['checked_at'])}" if row["checked_at"] else "")
        ]
        refs = [f"[$foreground-muted]źródło:[/] {escape(_source(row))}"]
        if row["ksef_number"]:
            refs.append(f"[$foreground-muted]numer KSeF:[/] [b]{escape(row['ksef_number'])}[/b]")
        if row["session_ref"]:
            refs.append(f"[$foreground-muted]sesja:[/] {escape(row['session_ref'])}")
        if row["invoice_ref"]:
            refs.append(f"[$foreground-muted]nr ref. faktury:[/] {escape(row['invoice_ref'])}")
        lines.append("   ".join(refs))
        if row["message"]:
            lines.append(escape(row["message"]))
        if row["status"] in UNRESOLVED_STATES:
            lines.append("[$text-warning]Nie wysyłaj tej faktury ponownie - naciśnij s, aby sprawdzić wynik.[/]")
        elif row["status"] == "accepted":
            lines.append("[$foreground-muted]Enter - podgląd faktury.[/]")
        elif self._draft_id(row) is not None:
            lines.append("[$foreground-muted]Enter - otwórz szkic, popraw i wyślij ponownie.[/]")
        self.query_one("#list-details", Static).update("\n".join(lines))

    def _draft_id(self, row: sqlite3.Row) -> int | None:
        draft = self.app.store.draft(row["draft_id"]) if row["draft_id"] else None
        return draft["id"] if draft is not None and not draft["archived"] else None

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        row = self.selected
        if action == "check":
            return any(r["status"] in UNRESOLVED_STATES for r in self.rows)
        if action == "upo":
            return bool(row and row["status"] == "accepted" and row["session_ref"])
        return True

    def show_outcome(self, text: str) -> None:
        status = self.query_one("#op-status", Static)
        status.update(text)
        status.display = bool(text)

    @on(DataTable.RowSelected, "#list")
    def _open(self) -> None:
        row = self.selected
        if row is None:
            return
        if row["status"] == "accepted":
            if self.app.store.get(row["ksef_number"], "issued") is None:
                self.notify("Faktury nie ma jeszcze w lokalnej bazie - zsynchronizuj listę (r).", timeout=5)
                return
            self.app.open_invoice(row["ksef_number"], "issued")
        elif (draft_id := self._draft_id(row)) is not None:
            self.app.open_draft(draft_id)

    @work(exclusive=True, group="sent")
    async def action_check(self) -> None:
        if await self.app.get_client() is None:
            return
        self.show_outcome("[$text-accent]⟳ Sprawdzam w KSeF wysyłki bez wyniku…[/]")
        await self.app.reconcile_all(ask=True).wait()
        self.reload()
        waiting = sum(r["status"] in UNRESOLVED_STATES for r in self.rows)
        if waiting:
            self.show_outcome(
                f"[$text-warning]Nadal bez wyniku: {waiting}. KSeF jeszcze przetwarza - sprawdź za chwilę.[/]"
            )
        else:
            self.show_outcome("[$text-success]✓ Wszystkie wysyłki mają już wynik.[/]")

    @work(exclusive=True, group="sent")
    async def action_upo(self) -> None:
        row = self.selected
        if row is None or row["status"] != "accepted":
            return
        record = self.app.store.sent_entry(row["id"])
        if record is None:
            return
        upo = record["upo"]
        if not upo:
            client = await self.app.get_client()
            if client is None:
                return
            try:
                upo = await fetch_upo(client, self.app.store, record)
            except KsefError as exc:
                text = upo_unavailable(exc, record["session_ref"])
                self.notify(text, title="UPO jeszcze niedostępne", severity="warning", timeout=12)
                return
        name = f"{safe_name(record['invoice_number'] or '')}_{record['ksef_number']}_UPO.xml"
        self.app.export_file(name, upo, title="UPO")
        self.reload()

    def action_sessions(self) -> None:
        from .account import SessionsScreen

        self.app.push_screen(SessionsScreen())


def _source(row: sqlite3.Row) -> str:
    return f"plik {row['file_name']}" if row["source"] == "import" else "wystawiona tutaj"

"""Wysyłka gotowych faktur XML z dysku (sesja interaktywna albo wsadowa) i pobieranie faktury po numerze KSeF."""

from __future__ import annotations

import glob
import re
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lxml import etree
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, Select, Static
from textual.worker import Worker, get_current_worker

from ..client import (
    AuthError,
    ConnectionInterrupted,
    InvoiceRejected,
    KsefClient,
    KsefError,
    NotSubmitted,
    OutcomeUnknown,
    RateLimitError,
    StillProcessing,
)
from ..config import os_error_text
from ..fa_parser import InvoiceView, parse_invoice
from ..invoice import is_fa3, validate_xml
from ..money import fmt_amount
from ..store import SENT_STATES, UNRESOLVED_STATES
from ..submissions import finalize_accepted, metadata_from_xml, reconcile
from .common import FormField, FormScreen, short_path, topbar

if TYPE_CHECKING:
    from .app import KsefApp

KSEF_NUMBER = re.compile(r"^\d{10}-\d{8}-[0-9A-F]{6}-?[0-9A-F]{6}-[0-9A-F]{2}$")


@dataclass
class XmlFile:
    path: Path
    xml: bytes
    view: InvoiceView | None
    errors: list[str] = field(default_factory=list)
    warning: str = ""
    state: str = ""  # stan ostatniej wysyłki z historii (store.SENT_STATES) albo "" - nie wysyłano
    message: str = ""
    ksef_number: str = ""
    sent_id: int | None = None
    historic: bool = False  # wpis z historii wysyłek, a nie plik wczytany teraz z dysku

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def ready(self) -> bool:
        """Można go wysłać: poprawny i nie czeka na wynik ani nie został przyjęty."""
        return self.ok and not self.historic and self.state in ("", "not_submitted", "rejected")

    @property
    def unresolved(self) -> bool:
        return self.state in UNRESOLVED_STATES and self.sent_id is not None

    def status(self, width: int = 0) -> Text:
        """Stan pliku słowami (kolor tylko pomaga); `width` skraca opis do tylu znaków."""
        text = self._status()
        if width and len(text) > width:
            text.truncate(width - 1)
            text.append("…")
        return text

    def _status(self) -> Text:
        message = f": {self.message}" if self.message else ""
        if self.state == "accepted":
            return Text(f"✓ przyjęta · {self.ksef_number}", style="green")
        if self.state == "sending":
            return Text("… wysyłanie", style="yellow")
        if self.state == "processing":
            return Text("… KSeF przetwarza - sprawdź status", style="yellow")
        if self.state == "unknown":
            return Text("? wynik nieznany - sprawdź status", style="yellow")
        if self.state == "rejected":
            return Text(f"✗ odrzucona{message}", style="red")
        if self.state == "not_submitted":
            return Text(f"nie wysłano{message}", style="yellow")
        if self.errors:
            return Text(f"✗ błąd: {self.errors[0]}", style="red")
        if self.warning:
            return Text(f"! {self.warning}", style="yellow")
        return Text("gotowa do wysłania", style="dim")


def _xml_paths(path: Path, pattern: str) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".xml")
    if any(ch in pattern for ch in "*?["):
        return sorted(Path(p) for p in glob.glob(str(path)))
    return [path]


def xml_paths(pattern: str) -> list[Path]:
    """Pliki do sprawdzenia: wskazany plik, pliki XML z katalogu albo pasujące do wzorca."""
    return _xml_paths(Path(pattern).expanduser(), pattern)


def load_file(p: Path, seller_nip: str) -> XmlFile:
    """Odczyt i sprawdzenie jednego pliku (składnia, schemat FA(3), sprzedawca)."""
    try:
        xml = p.read_bytes()
    except OSError as exc:
        return XmlFile(p, b"", None, [f"nie można odczytać pliku: {os_error_text(exc)}"])
    try:
        etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:
        return XmlFile(p, xml, None, [f"niepoprawny XML: {exc}"])
    if not is_fa3(xml):
        return XmlFile(p, xml, None, ["to nie jest faktura w schemacie FA(3)"])
    errors = validate_xml(xml)
    view = parse_invoice(xml)
    warning = ""
    if view.seller.nip and view.seller.nip != seller_nip:
        warning = f"sprzedawca {view.seller.nip} - wymaga uprawnień do wystawiania w jego imieniu"
    return XmlFile(p, xml, view, errors, warning)


def load_files(pattern: str, seller_nip: str) -> list[XmlFile]:
    return [load_file(p, seller_nip) for p in xml_paths(pattern)]


PROGRESS = {"session": "Łączenie z KSeF", "upload": "Przesyłanie", "close": "Zamykanie sesji"}


class ImportScreen(Screen):
    HELP = (
        "Wysyłka gotowych plików FA(3): wskaż plik albo folder, sprawdź listę i wyślij. Pliki już przyjęte "
        "albo czekające na wynik nie zostaną wysłane drugi raz."
    )
    VERTICAL_BREAKPOINTS = [(0, "-short"), (30, "-tall")]
    BINDINGS = [Binding("escape", "close", "Zamknij")]

    app: KsefApp

    def __init__(self) -> None:
        super().__init__()
        self.files: list[XmlFile] = []
        self.sending = False
        self.loading = False  # trwa sprawdzanie wczytanych plików
        self.load_note = ""  # wynik ostatniego wczytania (np. przerwano, pliki wysłane wcześniej)

    def compose(self) -> ComposeResult:
        yield from topbar("[b]Wysyłka plików XML[/b]", self.app)
        yield Static(
            "Wyślij faktury FA(3) przygotowane w innym programie. Podaj plik, katalog albo wzorzec "
            "(np. [b]~/faktury/*.xml[/b]). Każdy plik jest najpierw sprawdzany schematem XSD.",
            classes="screen-description",
        )
        with Horizontal(id="import-row"):
            yield Input(placeholder="~/faktury/2026-10/", id="import-path", compact=True)
            yield Button("Wczytaj", id="load", compact=True)
        with Horizontal(id="import-options"):
            yield Label("Tryb", classes="filter-label first")
            yield Select(
                [("interaktywny (jedna sesja)", "online"), ("wsadowy (paczka ZIP)", "batch")],
                value="online",
                allow_blank=False,
                id="mode",
                compact=True,
            )
            yield Checkbox("faktury wystawione offline", id="offline", compact=True)
        yield Static("", id="table-title")
        yield DataTable(cursor_type="row", zebra_stripes=True, id="files")
        yield Static("", id="file-details")
        yield Static("", id="import-status")
        with Horizontal(id="import-buttons"):
            yield Button("✓ Wyślij do KSeF", variant="success", id="send", disabled=True, compact=True)
            yield Button("Sprawdź status", id="recheck", variant="warning", compact=True)
            yield Button("Zamknij", id="close", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#files", DataTable)
        for label in ("Plik", "Numer", "Brutto", "Stan", "Nabywca"):
            table.add_column(label, key=label)
        self._show_history()
        self.query_one("#import-path", Input).focus()

    # --------------------------------------------------------------- pliki

    def _show_history(self) -> None:
        """Bez wczytanych plików: ostatnie wysyłki plików z historii, z ich aktualnym stanem."""
        files = []
        for row in self.app.store.import_history():
            view = None
            if row["xml"]:
                with suppress(etree.XMLSyntaxError, ValueError):
                    view = parse_invoice(row["xml"])
            path = Path(row["file_path"] or row["file_name"] or row["invoice_number"])
            files.append(
                XmlFile(
                    path,
                    row["xml"] or b"",
                    view,
                    state=row["status"],
                    message=row["message"] or "",
                    ksef_number=row["ksef_number"] or "",
                    sent_id=row["id"],
                    historic=True,
                )
            )
        self.files = files
        self._refresh_table()

    @on(Input.Submitted, "#import-path")
    @on(Button.Pressed, "#load")
    def _load(self) -> Worker[None] | None:
        """Sprawdza pliki w tle (schemat XSD bywa wolny przy setkach plików) - Esc przerywa."""
        pattern = self.query_one("#import-path", Input).value.strip()
        if not pattern:
            self.notify("Podaj plik albo katalog z fakturami XML.", severity="warning")
            return None
        if self.loading or self.sending:
            return None
        self.loading = True
        self._lock_loading()
        return self._check_files(pattern, self.app.profile.seller.nip)

    @work(exclusive=True, group="load", thread=True)
    def _check_files(self, pattern: str, seller_nip: str) -> None:
        worker = get_current_worker()
        paths = xml_paths(pattern)
        files = []
        for n, path in enumerate(paths, 1):
            if worker.is_cancelled:
                break
            self.app.call_from_thread(self._loading_progress, n, len(paths), path.name)
            files.append(load_file(path, seller_nip))
        if self.is_attached:  # przerwane sprawdzanie też pokazuje pliki sprawdzone do tej pory
            self.app.call_from_thread(self._loaded, files, len(paths))

    def _loading_progress(self, n: int, total: int, name: str) -> None:
        self.query_one("#import-status", Static).update(
            f"[$text-accent]⟳ Sprawdzam pliki: {n} z {total}[/]  [$foreground-muted]{escape(name)} · Esc przerywa[/]"
        )

    def _loaded(self, files: list[XmlFile], total: int) -> None:
        self.loading = False
        self._lock_loading()
        self.files = files
        previous = sum(self._attach_history(f) for f in self.files)
        notes = []
        if len(files) < total:
            notes.append(f"[$text-warning]Przerwano - sprawdzono {len(files)} z {total} plików.[/]")
        if not total:
            notes.append("[$text-warning]Nie znaleziono plików XML (*.xml) - sprawdź ścieżkę.[/]")
        if previous:
            notes.append(f"Wysłane już wcześniej: [b]{previous}[/b] - nie zostaną wysłane ponownie.")
        self.load_note = "  ".join(notes)
        self._refresh_table()
        if self.files:
            self.query_one("#files", DataTable).focus()

    def _lock_loading(self) -> None:
        for selector in ("#import-path", "#load", "#mode", "#offline"):
            self.query_one(selector).disabled = self.loading or self.sending

    def _attach_history(self, f: XmlFile) -> bool:
        """Łączy plik z wcześniejszą wysyłką (przyjętą albo w toku): tego samego XML albo faktury o tym numerze."""
        if not f.ok:
            return False
        store = self.app.store
        row = store.sent_for_xml(f.xml)
        message = ""
        if row is None and f.view and f.view.number:
            row = store.active_attempt(f.view.number, f.view.seller.nip or None)
            message = "faktura o tym numerze jest już wysłana (inny plik)"
        if row is None:
            return False
        f.sent_id, f.state, f.ksef_number = row["id"], row["status"], row["ksef_number"] or ""
        f.message = message or row["message"] or ""
        return True

    def _refresh_table(self) -> None:
        table = self.query_one("#files", DataTable)
        row = table.cursor_row
        table.clear()
        rows = []
        for f in self.files:
            view = f.view
            total = f"{fmt_amount(view.total)} {view.currency}" if view and view.total is not None else ""
            rows.append((f, f.path.name, view.number if view else "", total, view.buyer.name[:30] if view else ""))
        # Stan dostaje miejsce, które zostaje po nazwie pliku, numerze i kwocie; nabywca jest na końcu.
        fixed = sum(max((len(r[i]) for r in rows), default=6) + 2 for i in (1, 2, 3))
        width = max(18, self.size.width - 6 - fixed)
        for f, name, number, total, buyer in rows:
            table.add_row(name, number, Text(total, justify="right"), f.status(width), buyer)
        if self.files:
            table.move_cursor(row=min(row, len(self.files) - 1))
        historic = bool(self.files) and all(f.historic for f in self.files)
        title = "Poprzednie wysyłki plików" if historic else "Pliki do wysłania" if self.files else ""
        self.query_one("#table-title", Static).update(f"[b]{title}[/b]" if title else "")
        table.display = bool(self.files)
        self._update_details()

        ready = [f for f in self.files if f.ready]
        bad = [f for f in self.files if not f.ok]
        unresolved = [f for f in self.files if f.unresolved]
        if historic or not self.files:
            summary = "" if self.files else "[$foreground-muted]Wpisz ścieżkę i naciśnij Enter, aby wczytać pliki.[/]"
            summary = self.load_note or summary
        else:
            summary = f"[b]{len(self.files)}[/b] plików · gotowe do wysłania: [b]{len(ready)}[/b]"
            if bad:
                summary += f" · z błędami: [b $text-error]{len(bad)}[/] (zostaną pominięte)"
            if self.load_note:
                summary += f"\n{self.load_note}"
        if unresolved:
            summary += ("  ·  " if summary else "") + f"czeka na wynik: [b $text-warning]{len(unresolved)}[/]"
        if not self.sending:
            self.query_one("#import-status", Static).update(summary)
        button = self.query_one("#send", Button)
        button.disabled = not ready or self.sending or self.loading
        button.label = f"✓ Wyślij do KSeF ({len(ready)})" if ready else "✓ Wyślij do KSeF"
        self.query_one("#recheck", Button).display = bool(unresolved)
        self.query_one("#recheck", Button).disabled = self.sending

    @on(DataTable.RowHighlighted, "#files")
    def _update_details(self) -> None:
        table = self.query_one("#files", DataTable)
        details = self.query_one("#file-details", Static)
        if not self.files or table.cursor_row >= len(self.files):
            details.update("")
            return
        f = self.files[table.cursor_row]
        state = SENT_STATES.get(f.state, "").lower()
        lines = [f"[b]{escape(f.path.name)}[/b]" + (f"  [$foreground-muted]{escape(state)}[/]" if state else "")]
        lines += [f"[$text-error]• {escape(e)}[/]" for e in f.errors[:3]]
        if len(f.errors) > 3:
            lines.append(f"[$foreground-muted]… i {len(f.errors) - 3} więcej[/]")
        if f.message:
            lines.append(escape(f.message))
        elif f.ksef_number:
            lines.append(f"Numer KSeF: {escape(f.ksef_number)}")
        details.update("\n".join(lines))

    def _freeze(self) -> None:
        """Podczas wysyłki lista plików i tryb są zablokowane - wysyłamy dokładnie to, co widać."""
        for selector in ("#import-path", "#load", "#mode", "#offline"):
            self.query_one(selector).disabled = self.sending

    # ------------------------------------------------------------- wysyłka

    @on(Button.Pressed, "#send")
    def action_send(self) -> None:
        if not self.sending and any(f.ready for f in self.files):
            self.send()

    def _set_state(self, f: XmlFile, state: str, **fields: Any) -> None:
        assert f.sent_id is not None
        self.app.store.update_sent(f.sent_id, status=state, **fields)
        f.state = state
        if "message" in fields:
            f.message = fields["message"] or ""

    def _accepted(self, f: XmlFile, ksef_number: str, session_ref: str, invoice_ref: str | None) -> None:
        assert f.sent_id is not None
        exported = finalize_accepted(
            self.app.store,
            self.app.profile,
            f.sent_id,
            ksef_number,
            session_ref=session_ref,
            invoice_ref=invoice_ref,
            upo_attempted=False,
        )
        f.state, f.ksef_number, f.message = "accepted", ksef_number, ""
        if exported.error:
            f.message = f"nie zapisano kopii w {short_path(exported.folder)}: {exported.error}"

    @work(exclusive=True, group="import")
    async def send(self) -> None:
        ready = [f for f in self.files if f.ready]
        if not ready:
            return
        mode = self.query_one("#mode", Select).value
        offline = self.query_one("#offline", Checkbox).value
        if mode == "batch":
            duplicates = sorted(name for name, n in Counter(f.path.name for f in ready).items() if n > 1)
            if duplicates:
                self.notify(
                    f"W paczce nie mogą być dwa pliki o tej samej nazwie: {', '.join(duplicates)}. "
                    "Zmień nazwy plików albo wyślij je w trybie interaktywnym.",
                    title="Wysyłka wsadowa",
                    severity="error",
                    timeout=12,
                )
                return
        store, profile = self.app.store, self.app.profile
        # Wpisy w historii przed połączeniem: zamknięcie aplikacji w trakcie nie zgubi żadnego pliku.
        for f in ready:
            view = f.view
            f.sent_id = store.record_sent(
                invoice_number=(view.number if view else "") or f.path.name,
                status="sending",
                xml=f.xml,
                file_name=f.path.name,
                file_path=str(f.path),
                source="import",
                env=profile.env,
                context_id=profile.context_id,
                seller_nip=view.seller.nip if view else None,
                invoice_kind=view.kind_code if view else None,
            )
            f.state, f.message, f.ksef_number = "sending", "", ""
        ids = {f.sent_id for f in ready if f.sent_id is not None}
        self.app.active_sends |= ids
        self.sending = True
        self._freeze()
        self._refresh_table()
        session_ref = ""
        try:
            client = await self.app.get_client()
            if client is None:
                for f in ready:
                    self._set_state(f, "not_submitted", message="nie połączono z KSeF")
                return
            if mode == "batch":
                session_ref = await self._send_batch(client, ready, offline)
            else:
                session_ref = await self._send_online(client, ready, offline)
        finally:
            self.app.active_sends -= ids
            self.sending = False
            if self.is_attached:
                self._freeze()
                self._refresh_table()
                self._report(ready, session_ref)
                # Wyłączony „Wyślij” oddałby fokus „Zamknij” - Enter zamknąłby ekran z wynikami.
                self.query_one("#files", DataTable).focus()
            if any(f.state == "accepted" for f in ready):
                self.app.refresh_main()

    def _progress(self, step: str, state: str) -> None:
        label = PROGRESS.get(step, step)
        if step == "upload" and "/" in state and ":" in state:
            numbers, _, name = state.partition(": ")
            done, total = numbers.split("/")
            text = f"Przesyłanie {done} z {total}: {name}"
        else:
            text = f"{label}…" if state in ("start", "ok") else f"{label}: {state}"
        self.query_one("#import-status", Static).update(f"[$text-accent]⟳ {escape(text)}[/]")

    async def _send_online(self, client: KsefClient, ready: list[XmlFile], offline: bool) -> str:
        store = self.app.store

        def on_session(ref: str) -> None:
            for f in ready:
                store.update_sent(f.sent_id, session_ref=ref)

        def on_refs(index: int, reference: str) -> None:
            self._set_state(ready[index], "processing", invoice_ref=reference)  # plik jest już w KSeF

        payload = [(f.path.name, f.xml) for f in ready]
        try:
            session_ref, results = await client.send_invoices_online(
                payload, self._progress, offline=offline, on_session=on_session, on_refs=on_refs
            )
        except KsefError as exc:
            self._interrupted(ready, exc)
            return ""
        for f in ready:
            store.update_sent(f.sent_id, session_ref=session_ref)
        for f, (_, reference, error) in zip(ready, results, strict=True):
            if isinstance(error, OutcomeUnknown | ConnectionInterrupted):
                self._set_state(f, "unknown", message="połączenie przerwane w trakcie przesyłania")
            elif error is not None:
                self._set_state(f, "not_submitted", message=error.message)
            elif reference and f.state != "processing":
                self._set_state(f, "processing", invoice_ref=reference)
        self._refresh_table()
        for f, (_, reference, error) in zip(ready, results, strict=True):
            if error is not None or not reference:
                continue
            self.query_one("#import-status", Static).update(f"[$text-accent]⟳ KSeF sprawdza {escape(f.path.name)}…[/]")
            try:
                info = await client.wait_for_invoice(session_ref, reference, timeout=90)
            except StillProcessing:
                continue
            except InvoiceRejected as exc:
                self._set_state(f, "rejected", message=exc.message)
            except (RateLimitError, AuthError) as exc:
                self.notify(
                    f"{exc.human()}\nPrzesłane pliki są w KSeF - ich status sprawdzisz później (Sprawdź status).",
                    title="Nie sprawdzono wszystkich plików",
                    severity="warning",
                    timeout=12,
                )
                break
            except KsefError:
                continue  # plik jest w KSeF, status sprawdzimy później
            else:
                self._accepted(f, info["ksefNumber"], session_ref, reference)
            self._refresh_table()
        return session_ref

    async def _send_batch(self, client: KsefClient, ready: list[XmlFile], offline: bool) -> str:
        store = self.app.store

        def on_session(ref: str) -> None:
            for f in ready:
                store.update_sent(f.sent_id, session_ref=ref)

        payload = [(f.path.name, f.xml) for f in ready]
        try:
            session_ref = await client.send_batch(payload, self._progress, offline=offline, on_session=on_session)
        except OutcomeUnknown as exc:
            for f in ready:
                self._set_state(
                    f, "unknown", session_ref=exc.session_ref, message="połączenie przerwane przy zamykaniu paczki"
                )
            return exc.session_ref
        except KsefError as exc:
            self._interrupted(ready, exc)
            return ""
        for f in ready:
            self._set_state(f, "processing", session_ref=session_ref)  # paczka jest w KSeF
        self._refresh_table()
        self.query_one("#import-status", Static).update("[$text-accent]⟳ KSeF przetwarza paczkę…[/]")
        try:
            await client.wait_for_session(session_ref)
            items = await client.session_invoices(session_ref)
        except KsefError as exc:
            self.notify(
                f"{exc.human()}\nPaczka jest w KSeF - wynik sprawdzisz później (Sprawdź status).",
                title="Wynik paczki jeszcze niedostępny",
                severity="warning",
                timeout=12,
            )
            return session_ref
        by_name = {i.get("invoiceFileName"): i for i in items}
        for f in ready:
            self._apply_result(f, by_name.get(f.path.name), session_ref)
        return session_ref

    def _interrupted(self, files: list[XmlFile], exc: KsefError) -> None:
        """Błąd całej wysyłki: bez otwartej sesji nic nie dotarło do KSeF, z otwartą - wynik nieznany."""
        for f in files:
            row = self.app.store.sent_entry(f.sent_id) if f.sent_id is not None else None
            if isinstance(exc, NotSubmitted) or not (row and row["session_ref"]):
                self._set_state(f, "not_submitted", message=exc.message)
            else:
                self._set_state(f, "unknown", message=exc.message)

    def _apply_result(self, f: XmlFile, info: dict[str, Any] | None, session_ref: str) -> None:
        if not info:
            self._set_state(f, "unknown", message="brak pliku w wynikach sesji")
            return
        code = info["status"]["code"]
        reference = info.get("referenceNumber")
        if reference:
            self.app.store.update_sent(f.sent_id, invoice_ref=reference)
        if code in (100, 150):
            self._set_state(f, "processing")
        elif code == 200 and info.get("ksefNumber"):
            self._accepted(f, info["ksefNumber"], session_ref, reference)
        else:
            details = "; ".join(info["status"].get("details") or [])
            self._set_state(f, "rejected", message=f"{info['status'].get('description', '')} {details}".strip())

    def _report(self, sent: list[XmlFile], session_ref: str) -> None:
        counts = Counter(f.state for f in sent)
        accepted = counts["accepted"]
        color = "$text-success" if accepted == len(sent) else "$text-warning"
        parts = [f"[b {color}]Przyjęto {accepted} z {len(sent)}[/]"]
        for state, label in (
            ("processing", "KSeF przetwarza"),
            ("unknown", "wynik nieznany"),
            ("rejected", "odrzucone"),
            ("not_submitted", "nie wysłano"),
        ):
            if counts[state]:
                parts.append(f"{label}: [b]{counts[state]}[/b]")
        text = " · ".join(parts)
        if session_ref:
            text += f"\n[$foreground-muted]sesja {escape(session_ref)} · UPO: Wysyłki i UPO (w)[/]"
        self.query_one("#import-status", Static).update(text)

    # ------------------------------------------------------- sprawdzanie stanu

    @on(Button.Pressed, "#recheck")
    def _recheck_pressed(self) -> None:
        self.recheck()

    @work(exclusive=True, group="import")
    async def recheck(self) -> None:
        pending = [f for f in self.files if f.unresolved and f.sent_id not in self.app.active_sends]
        if not pending:
            return
        client = await self.app.get_client()
        if client is None:
            return
        store = self.app.store
        sessions: dict[str, list[dict[str, Any]]] = {}
        self.query_one("#import-status", Static).update("[$text-accent]⟳ Sprawdzam status w KSeF…[/]")
        accepted = False
        for f in pending:
            row = store.sent_entry(f.sent_id)
            if row is None:
                continue
            try:
                result = await reconcile(client, store, self.app.profile, row, sessions)
            except (RateLimitError, AuthError) as exc:
                self.notify(exc.human(), title="Sprawdzanie statusu", severity="warning", timeout=10)
                break
            f.state = result.status
            f.message = result.message if result.status in ("rejected", "not_submitted") else f.message
            if result.status == "accepted":
                f.ksef_number, accepted = result.ksef_number, True
        self._refresh_table()
        if accepted:
            self.app.refresh_main()

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        if self.sending:
            self.notify("Poczekaj na zakończenie wysyłki.", severity="warning", timeout=3)
            return
        if self.loading:
            self.workers.cancel_group(self, "load")  # Esc najpierw przerywa sprawdzanie plików
            return
        self.app.pop_screen()


def normalize_ksef_number(value: str) -> str:
    """Numer KSeF wklejony z odstępami, małymi literami albo w cudzysłowie - w postaci kanonicznej."""
    return re.sub(r"[\s\"'„”]+", "", value).upper()


def fetch_invoice_dialog(app: KsefApp) -> None:
    """Pobiera dowolną fakturę po numerze KSeF (do której mamy dostęp) i otwiera jej podgląd."""

    def check(value: str) -> str | None:
        ok = KSEF_NUMBER.match(normalize_ksef_number(value))
        return None if ok else "format: NIP-RRRRMMDD-XXXXXX-XXXXXX-XX"

    def entered(values: dict[str, Any] | None) -> None:
        if values:
            fetch_invoice(app, normalize_ksef_number(values["ksef"]))

    app.push_screen(
        FormScreen(
            "Pobierz fakturę z KSeF",
            [FormField("ksef", "Numer KSeF", required=True, check=check, placeholder="1234567890-20260101-…")],
            intro="Faktura trafi do lokalnej bazy - wystawiona, otrzymana, jako podmiot trzeci albo w innej roli.",
            submit="Pobierz",
            submit_on_enter=True,
        ),
        entered,
    )


def invoice_role(app: KsefApp, xml: bytes) -> str:
    """Rola firmy na fakturze: wystawca, nabywca, podmiot trzeci albo inna (np. faktura udostępniona)."""
    profile = app.profile
    ours = {profile.seller.nip, profile.context_id}
    view = parse_invoice(xml)
    if view.seller.nip in ours:
        return "issued"
    if view.buyer.nip in ours:
        return "received"
    if any(third.nip in ours for third in view.third_parties if third.nip):
        return "third"
    return "other"


def fetch_invoice(app: KsefApp, ksef_number: str) -> None:
    async def run() -> None:
        store = app.store
        cached = store.find(ksef_number)
        if cached is not None and cached["xml"]:
            # Już jest w lokalnej bazie - bez zapytania do KSeF.
            app.notify("Ta faktura jest już w lokalnej bazie.", timeout=4)
            _opened(app, ksef_number, cached["direction"])
            return
        client = await app.get_client()
        if client is None:
            return
        try:
            xml = await client.get_invoice(ksef_number)
        except KsefError as exc:
            app.notify(exc.human(), title="Pobieranie faktury", severity="error", timeout=10)
            return
        if cached is not None:
            store.set_xml(ksef_number, cached["direction"], xml)  # metadane już były - dochodzi treść
            direction = cached["direction"]
        else:
            direction = invoice_role(app, xml)
            store.upsert_metadata(direction, [metadata_from_xml(xml, ksef_number)], {ksef_number: xml})
        _opened(app, ksef_number, direction)

    app.run_worker(run(), group="fetch", exclusive=True)


def _opened(app: KsefApp, ksef_number: str, direction: str) -> None:
    from .main_screen import TAB_LABELS, MainScreen

    app.refresh_main(select=f"{direction}|{ksef_number}")
    main = next((s for s in app.screen_stack if isinstance(s, MainScreen)), None)
    if main is not None and main.is_mounted and not main.shows(ksef_number, direction):
        # Bez tego użytkownik szukałby faktury na liście, na której jej nie widać.
        where = TAB_LABELS.get(direction, "Inna rola")
        hint = "Wszystkie" if direction == "other" else where
        app.notify(f"Obecne filtry albo zakładka ukrywają tę fakturę na liście (jest w: {hint}).", timeout=8)
    app.open_invoice(ksef_number, direction)

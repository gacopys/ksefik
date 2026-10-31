"""Podgląd faktury oraz przepływ wystawiania: szybka faktura → podsumowanie ⇄ edycja → wysyłka do KSeF."""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Callable
from contextlib import suppress
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from lxml import etree
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.markup import escape
from textual.screen import ModalScreen, Screen
from textual.validation import Function
from textual.widgets import Button, Footer, Input, Label, Select, Static

from ..client import (
    AuthError,
    InvoiceRejected,
    KsefError,
    NotSubmitted,
    OutcomeUnknown,
    RateLimitError,
    StillProcessing,
)
from ..crypto import sha256_b64
from ..fa_parser import InvoiceView, PartyView, RateView, draft_gaps, parse_draft, parse_invoice
from ..invoice import Draft, Line, as_correction, as_new, build_xml, check_draft, new_draft, validate_xml
from ..money import NBSP, fmt_amount, fmt_date, parse_amount, parse_date
from ..profile import Party
from ..store import SENT_STATES
from ..submissions import Exported, fetch_upo, finalize_accepted, reconcile
from .common import ConfirmScreen, draft_saved_text, safe_name, short_path, topbar
from .widgets import InvoiceDocument, amount_preview, link

if TYPE_CHECKING:
    from .app import KsefApp

NEW_BUYER = "__new__"
OTHER_ITEM = "__other__"
KIND_GENITIVE = {
    "KOR": "korygującej",
    "ZAL": "zaliczkowej",
    "ROZ": "rozliczeniowej",
    "UPR": "uproszczonej",
    "KOR_ZAL": "korygującej fakturę zaliczkową",
    "KOR_ROZ": "korygującej fakturę rozliczeniową",
}


def upo_unavailable(exc: KsefError, session_ref: str) -> str:
    return (
        f"{exc.human()}\n\nUPO bywa dostępne dopiero po zamknięciu sesji - spróbuj za kilka minut. "
        f"Znajdziesz je też w Wysyłki i UPO → Sesje w KSeF (sesja {session_ref})."
    )


def verify_url(qr_base: str, seller_nip: str, issue_date: date | None, invoice_hash_b64: str | None) -> str:
    if not (issue_date and invoice_hash_b64):
        return ""
    digest = base64.b64decode(invoice_hash_b64)
    token = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"{qr_base}/invoice/{seller_nip}/{issue_date:%d-%m-%Y}/{token}"


# --------------------------------------------------------------------------- podgląd


class InvoiceScreen(Screen):
    HELP = (
        "Podgląd faktury z KSeF. Zapisz XML lub UPO, skopiuj numer KSeF albo otwórz stronę weryfikacji. "
        "Na podstawie zwykłej faktury VAT wystawisz podobną albo korektę."
    )
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Wróć do listy"),
        Binding("c", "copy", "Kopiuj nr KSeF"),
        Binding("x", "save_xml", "Zapisz XML"),
        Binding("o", "open_verify", "Otwórz weryfikację KSeF"),
        Binding("l", "copy_link", "Kopiuj link weryfikacji", show=False),
        Binding("d", "duplicate", "Wystaw podobną"),
        Binding("k", "correct", "Korekta"),
        Binding("u", "upo", "Pobierz UPO"),
        Binding("g", "download", "Pobierz treść"),
    ]

    app: KsefApp

    def __init__(self, row: sqlite3.Row) -> None:
        super().__init__()
        self.row = row
        meta = json.loads(row["metadata"] or "{}")
        issue = parse_date(row["issue_date"] or "")
        self.url = verify_url(self.app.profile.qr_url, row["seller_nip"], issue, meta.get("invoiceHash"))

    @property
    def kind_code(self) -> str:
        """Rodzaj faktury (VAT, KOR, ZAL, ROZ, UPR, KOR_ZAL, KOR_ROZ) - z treści, a bez niej z metadanych."""
        if self.row["xml"]:
            return parse_invoice(self.row["xml"]).kind_code
        return (self.row["invoice_type"] or "").upper()

    @property
    def reuse_blocked(self) -> str:
        """Dlaczego tej faktury nie da się skopiować ani skorygować w aplikacji (puste - da się)."""
        if self.row["direction"] != "issued" or not self.row["xml"] or self.kind_code == "VAT":
            return ""
        kind = KIND_GENITIVE.get(self.kind_code, f"rodzaju {self.kind_code}")
        return (
            f"Wystawianie podobnej i korekta faktury {kind} nie są jeszcze obsługiwane - "
            "zapisz XML (x) i przygotuj dokument w innym programie."
        )

    def compose(self) -> ComposeResult:
        row = self.row
        labels = {
            "issued": "Faktura wystawiona",
            "received": "Faktura otrzymana",
            "third": "Faktura (podmiot trzeci)",
            "authorized": "Faktura (podmiot upoważniony)",
        }
        label = labels.get(row["direction"], "Faktura (inna rola)")
        yield from topbar(f"[b]{label}[/b]  {escape(row['invoice_number'] or '')}", self.app)
        if self.reuse_blocked:
            yield Static(f"[$text-warning]{escape(self.reuse_blocked)}[/]", classes="notice")
        if row["xml"]:
            view = parse_invoice(row["xml"])
        else:
            view = self._view_from_metadata()
            yield Static(
                "[$text-warning]Treść faktury nie została jeszcze pobrana - widzisz dane z metadanych KSeF. "
                "Naciśnij g, aby ją pobrać.[/]",
                classes="notice",
            )
        yield InvoiceDocument(view, ksef_number=row["ksef_number"], verify_url=self.url, id="document")
        yield Footer()

    def _view_from_metadata(self) -> InvoiceView:
        row = self.row
        view = InvoiceView(
            number=row["invoice_number"] or "",
            kind=row["invoice_type"] or "",
            currency=row["currency"] or "PLN",
            issue_date=parse_date(row["issue_date"] or ""),
            seller=PartyView(name=row["seller_name"] or "", nip=row["seller_nip"] or ""),
            buyer=PartyView(name=row["buyer_name"] or "", nip=row["buyer_id"] or ""),
        )
        view.rates = [RateView("Razem", Decimal(str(row["net"] or 0)), Decimal(str(row["vat"] or 0)))]
        view.total = Decimal(str(row["gross"] or 0))
        return view

    def on_mount(self) -> None:
        self.query_one("#document").focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        has_xml = bool(self.row["xml"])
        if action == "save_xml":
            return has_xml
        if action in ("duplicate", "correct"):
            if not has_xml or self.row["direction"] != "issued":
                return False
            return None if self.reuse_blocked else True  # widoczne, ale wyszarzone - powód w ramce u góry
        if action == "download":
            return not has_xml
        if action == "upo":
            return self.row["direction"] == "issued"
        return True

    def action_copy(self) -> None:
        self.app.copy_text(self.row["ksef_number"], "numer KSeF")

    def action_save_xml(self) -> None:
        name = f"{safe_name(self.row['invoice_number'] or '')}_{self.row['ksef_number']}.xml"
        self.app.export_file(name, self.row["xml"], title="XML faktury")

    def action_open_verify(self) -> None:
        if self.url:
            self.app.action_open_link(self.url)
        else:
            self.notify("Brak danych do zbudowania linku weryfikacyjnego.", severity="warning")

    def action_copy_link(self) -> None:
        if self.url:
            self.app.copy_text(self.url, "link weryfikacyjny")
        else:
            self.notify("Brak danych do zbudowania linku weryfikacyjnego.", severity="warning")

    def _reuse(self, title: str, make: Callable[[Draft], Draft]) -> None:
        """Projekt na podstawie tej faktury - po ostrzeżeniu, jeśli część jej danych nie zostanie przeniesiona."""
        if self.reuse_blocked:
            self.notify(self.reuse_blocked, severity="warning", timeout=8)
            return
        try:
            draft = parse_draft(self.row["xml"])
            gaps = draft_gaps(self.row["xml"])
        except (ValueError, TypeError, etree.XMLSyntaxError) as exc:
            self.notify(f"Nie udało się odczytać faktury: {exc}", severity="error")
            return

        def go(ok: bool | None = True) -> None:
            if ok:
                self.app.open_editor(make(draft), source_ksef=self.row["ksef_number"])

        if not gaps:
            go()
            return
        self.app.push_screen(
            ConfirmScreen(
                title,
                "Ta faktura zawiera dane, których nowy dokument nie przejmie:\n"
                + "\n".join(f"• {escape(gap)}" for gap in gaps)
                + "\n\n[$foreground-muted]Jeśli są potrzebne, przygotuj dokument w innym programie.[/]",
                confirm="Kontynuuj bez tych danych",
                danger=False,
                focus_cancel=True,
            ),
            go,
        )

    def action_duplicate(self) -> None:
        store = self.app.store
        self._reuse(
            "Wystaw podobną fakturę",
            lambda draft: as_new(draft, self.app.profile, date.today(), store.reserved_numbers()),
        )

    def action_correct(self) -> None:
        store = self.app.store
        self._reuse(
            "Korekta faktury",
            lambda draft: as_correction(
                draft, self.row["ksef_number"], self.app.profile, date.today(), store.reserved_numbers()
            ),
        )

    @work(exclusive=True, group="invoice")
    async def action_upo(self) -> None:
        record = self.app.store.sent_record(self.row["ksef_number"])
        if record is None or not record["session_ref"]:
            self.notify(
                "Ta faktura nie była wysłana z tej aplikacji. UPO znajdziesz w Wysyłki i UPO → Sesje w KSeF.",
                severity="warning",
                timeout=8,
            )
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
        self.app.export_file(
            f"{safe_name(self.row['invoice_number'] or '')}_{self.row['ksef_number']}_UPO.xml", upo, title="UPO"
        )

    @work(exclusive=True, group="invoice")
    async def action_download(self) -> None:
        client = await self.app.get_client()
        if client is None:
            return
        try:
            xml = await client.get_invoice(self.row["ksef_number"])
        except KsefError as exc:
            self.notify(exc.human(), title="Pobieranie faktury", severity="error")
            return
        store = self.app.store
        store.set_xml(self.row["ksef_number"], self.row["direction"], xml)
        self.row = store.get(self.row["ksef_number"], self.row["direction"])
        await self.recompose()
        self.query_one("#document").focus()
        self.refresh_bindings()


# ---------------------------------------------------------------------- szybka faktura


class QuickInvoiceScreen(ModalScreen[Draft | tuple[str, Draft] | None]):
    """Kontrahent, pozycja i kwota - reszta z profilu. Zwraca projekt albo ("edit", projekt) do edycji."""

    HELP = (
        "Szybka faktura: wybierz kontrahenta i pozycję, wpisz kwotę netto - resztę (numer, daty, rachunek) "
        "podpowiada profil. „Dalej” pokazuje podsumowanie przed wysyłką; nic nie trafia do KSeF bez potwierdzenia."
    )
    BINDINGS = [Binding("escape", "cancel", "Anuluj"), Binding("ctrl+o", "more", "Więcej opcji")]

    app: KsefApp

    def __init__(self) -> None:
        super().__init__()
        profile = self.app.profile
        self.touched: set[str] = set()  # pola zmienione ręcznie - zmiana kontrahenta ich nie nadpisuje
        self._ready = False
        self._currency = ""
        self.issued = self.app.numbers_in_use()
        self.buyers: dict[str, Party] = {}
        for contractor in sorted(profile.contractors, key=lambda c: not c.domyslny):
            self.buyers.setdefault(contractor.party.key, contractor.party)
        for xml in self.app.store.recent_buyer_invoices():
            try:
                buyer = parse_draft(xml).buyer
            except (ValueError, TypeError):
                continue
            self.buyers.setdefault(buyer.key, buyer)
        self.items = {item.nazwa: item for item in sorted(profile.items, key=lambda i: not i.domyslna)}

    def compose(self) -> ComposeResult:
        buyer_options = [(f"{p.nazwa[:44]}  ({p.identifier or 'bez id.'})", k) for k, p in self.buyers.items()]
        buyer_options.append(("＋ Nowy kontrahent (uzupełnisz w edycji)", NEW_BUYER))
        item_options = [(name, name) for name in self.items] + [("＋ Inna pozycja (uzupełnisz w edycji)", OTHER_ITEM)]
        with Vertical(id="amount-dialog", classes="dialog"):
            yield Static("[b]Nowa faktura[/b]", classes="dialog-title")
            with Horizontal(classes="quick-row"):
                yield Label("Kontrahent", classes="quick-label")
                yield Select(buyer_options, value=buyer_options[0][1], allow_blank=False, id="buyer", compact=True)
            with Horizontal(classes="quick-row"):
                yield Label("Pozycja", classes="quick-label")
                yield Select(item_options, value=item_options[0][1], allow_blank=False, id="item", compact=True)
            yield Label("", id="amount-label", classes="field-label")
            yield Input(
                placeholder="np. 12 300,00",
                id="amount",
                validators=[Function(lambda v: (parse_amount(v) or 0) > 0, "Podaj kwotę większą od zera")],
            )
            yield Static("", id="amount-preview")
            yield Static("", id="changed")
            yield Static("", id="provenance", classes="dialog-text")
            with Horizontal(classes="dialog-buttons"):
                yield Button("Więcej opcji…", id="more")
                yield Button("Anuluj", id="cancel")
                yield Button("Dalej →", variant="primary", id="next")

    def on_mount(self) -> None:
        self._apply_item()
        self._refresh()
        self.query_one("#amount", Input).focus()
        self.call_after_refresh(self._mark_ready)

    def _mark_ready(self) -> None:
        self._ready = True  # zdarzenia z inicjalizacji pól to jeszcze nie zmiany użytkownika

    def _draft(self) -> Draft:
        buyer_key = self.query_one("#buyer", Select).value
        item_name = self.query_one("#item", Select).value
        buyer = self.buyers.get(str(buyer_key)) if buyer_key != NEW_BUYER else Party()
        item = self.items.get(str(item_name))
        amount = parse_amount(self.query_one("#amount", Input).value)
        draft = new_draft(self.app.profile, date.today(), self.issued, buyer=buyer, item=item, net=amount)
        if item is None:
            draft = draft.with_changes(lines=(Line(name="", unit_price=amount or Decimal(0)),))
        return draft

    @on(Select.Changed, "#buyer")
    def _buyer_changed(self, event: Select.Changed) -> None:
        """Nowy kontrahent podpowiada swoją pozycję, cenę i walutę - ale nie nadpisuje tego, co zmieniono ręcznie."""
        buyer = self.buyers.get(str(event.value))
        contractor = self.app.profile.contractor(buyer) if buyer else None
        changed = []
        item = self.query_one("#item", Select)
        if (
            "item" not in self.touched
            and contractor
            and contractor.pozycja in self.items
            and item.value != contractor.pozycja
        ):
            with self.prevent(Select.Changed):
                item.value = contractor.pozycja
            changed.append(f"pozycja „{contractor.pozycja}”")
        changed += self._apply_item()
        currency = self._draft().currency
        if currency != self._currency:
            changed.append(f"waluta {currency}")
        text = ("[$foreground-muted]Zmieniono: " + escape(", ".join(changed)) + "[/]") if changed else ""
        self.query_one("#changed", Static).update(text)
        self._refresh()

    @on(Select.Changed, "#item")
    def _item_changed(self) -> None:
        if self._ready:
            self.touched.add("item")
        self._apply_item()
        self._refresh()

    def _apply_item(self) -> list[str]:
        """Cena z katalogu trafia do kwoty, dopóki kwoty nie wpisano ręcznie."""
        if "amount" in self.touched:
            return []
        item = self.items.get(str(self.query_one("#item", Select).value))
        amount = self.query_one("#amount", Input)
        value = fmt_amount(item.cena_netto).replace(NBSP, " ") if item and item.cena_netto else ""
        if value == amount.value:
            return []
        with self.prevent(Input.Changed):
            amount.value = value
        return [f"kwota {value}"] if value else []

    @on(Input.Changed, "#amount")
    def _amount_typed(self) -> None:
        if self._ready:
            self.touched.add("amount")
        self._refresh()

    def _refresh(self) -> None:
        draft = self._draft()
        self._currency = draft.currency
        rate = draft.lines[0].vat_rate if draft.lines else "23"
        net = parse_amount(self.query_one("#amount", Input).value)
        self.query_one("#amount-label", Label).update(f"Kwota netto ({draft.currency})")
        self.query_one("#amount-preview", Static).update(amount_preview(net, rate, draft.currency))
        payment = draft.payment
        parts = [f"nr {escape(draft.number)}", f"wystawienie {fmt_date(draft.issue_date)}"]
        if payment.due_date:
            parts.append(f"termin {fmt_date(payment.due_date)} ({(payment.due_date - draft.issue_date).days} dni)")
        if payment.accounts:
            account = payment.accounts[0]
            parts.append(f"rachunek …{account.number[-4:]}" + (f" {account.waluta}" if account.waluta else ""))
        self.query_one("#provenance", Static).update(
            "[$foreground-muted]Z profilu: " + " · ".join(parts) + ". Zmienisz je w podsumowaniu (e).[/]"
        )

    @on(Input.Submitted, "#amount")
    @on(Button.Pressed, "#next")
    def _next(self) -> None:
        amount = parse_amount(self.query_one("#amount", Input).value)
        if amount is None or amount <= 0:
            self.notify("Wpisz kwotę netto większą od zera, np. 12 300,00", severity="warning", timeout=4)
            self.query_one("#amount", Input).focus()
            return
        draft = self._draft()
        needs_edit = self.query_one("#buyer", Select).value == NEW_BUYER or not draft.lines[0].name
        self.dismiss(("edit", draft) if needs_edit else draft)

    @on(Button.Pressed, "#more")
    def action_more(self) -> None:
        self.dismiss(("edit", self._draft()))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        # Wpisana kwota to już początek faktury - zostaje jako szkic (porzucisz go w zakładce Szkice).
        amount = parse_amount(self.query_one("#amount", Input).value)
        if amount and amount > 0:
            self.app.save_draft(self._draft())
            self.app.notify(draft_saved_text(), timeout=5)
            self.app.refresh_main()
        self.dismiss(None)


# ---------------------------------------------------------------------- podsumowanie


class SummaryScreen(Screen):
    HELP = (
        "Faktura dokładnie w takiej postaci, w jakiej trafi do KSeF. Wysyła ją tylko przycisk „Wyślij do KSeF” "
        "(F9 przenosi na niego, Enter naciska). Faktura jest zapisana jako szkic - możesz wrócić do niej później."
    )
    BINDINGS = [
        # Żaden skrót nie wysyła faktury: F9 tylko przenosi na przycisk „Wyślij do KSeF”, Enter go naciska.
        Binding("f9", "focus_send", "Do wysyłki"),
        Binding("e", "edit", "Edytuj"),
        Binding("x", "save_xml", "Zapisz XML"),
        Binding("delete", "discard", "Porzuć szkic", key_display="Del"),
        Binding("escape", "close", "Zamknij"),
    ]

    app: KsefApp

    def __init__(self, draft: Draft, *, draft_id: int | None = None) -> None:
        super().__init__()
        self.sending = False
        self.draft_id = draft_id
        self._set_draft(draft)

    def _set_draft(self, draft: Draft) -> None:
        """Buduje XML i sprawdza fakturę - podsumowanie pokazuje dokładnie to, co trafi do KSeF."""
        store = self.app.store
        self.draft = draft
        self.xml = build_xml(draft)
        reserved = set(store.reserved_numbers(draft.seller.nip))
        blocking: list[str] = []
        attempt = store.active_attempt(draft.number, draft.seller.nip)
        if attempt is not None:
            reserved.discard(draft.number.strip())
            if attempt["status"] == "accepted":
                blocking.append(
                    f"Faktura {draft.number} jest już przyjęta przez KSeF (numer KSeF {attempt['ksef_number']}). "
                    "Aby wystawić kolejną, zmień numer w edycji (e)."
                )
            else:
                blocking.append(
                    f"Faktura {draft.number} została już wysłana - stan: {SENT_STATES[attempt['status']].lower()}. "
                    "Nie wysyłaj jej ponownie: aplikacja sprawdzi wynik w KSeF."
                )
        issues = check_draft(draft, date.today(), reserved)
        self.blocking = blocking + [i.message for i in issues if i.blocking]
        self.blocking += [f"Błąd struktury FA(3): {e}" for e in validate_xml(self.xml)]
        self.warnings = [i.message for i in issues if not i.blocking]

    def compose(self) -> ComposeResult:
        draft = self.draft
        title = "Podsumowanie korekty" if draft.kind == "KOR" else "Podsumowanie faktury"
        yield from topbar(f"[b]{title}[/b]  {escape(draft.number)}", self.app)
        if self.blocking:
            yield Static(
                "[b]Tej faktury nie można jeszcze wysłać:[/b]\n" + "\n".join(f"• {escape(m)}" for m in self.blocking),
                classes="notice error",
            )
        if self.warnings:
            yield Static("\n".join(f"• {escape(m)}" for m in self.warnings), classes="notice warning")
        yield InvoiceDocument(parse_invoice(self.xml), id="document")
        with Horizontal(id="summary-buttons"):
            yield Button("✓ Wyślij do KSeF", variant="success", id="send", disabled=bool(self.blocking))
            yield Button("✎ Edytuj", variant="primary", id="edit")
            yield Button("Zamknij", id="close")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#edit" if self.blocking else "#document").focus()
        # Podsumowanie to też szkic: zamknięcie aplikacji albo ekranu nie gubi faktury.
        try:
            self.draft_id = self.app.save_draft(self.draft, self.draft_id)
        except sqlite3.Error as exc:
            self.notify(f"Nie udało się zapisać szkicu: {exc}", severity="error", timeout=10)

    def _reshow(self) -> None:
        self.refresh(recompose=True)
        self.call_after_refresh(self.on_mount)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "focus_send" and self.blocking:
            return None  # widoczne, ale wyszarzone
        return True

    def action_focus_send(self) -> None:
        self.query_one("#send", Button).focus()
        self.notify("Naciśnij Enter, aby wysłać fakturę do KSeF.", timeout=4)

    @on(Button.Pressed, "#send")
    def action_send(self) -> None:
        if self.sending:
            return
        # Od otwarcia podsumowania mogło się coś zmienić (data, numer zajęty w innej wysyłce) - sprawdzamy jeszcze raz.
        self._set_draft(self.draft)
        if self.blocking:
            self.notify("Tej faktury nie można wysłać - popraw zaznaczone problemy.", severity="warning")
            self._reshow()
            return
        self.sending = True
        self.query_one("#send", Button).disabled = True
        screen = SendScreen(self.draft, self.xml, draft_id=self.draft_id)

        def done(_: str | None) -> None:
            self.sending = False
            if screen.outcome == "accepted":
                self.app.back_to_main(select=f"issued|{screen.ksef_number}")
            elif screen.outcome in ("sending", "processing", "unknown"):
                # Faktura jest już w KSeF albo mogła do niego dotrzeć - z podsumowania nie wolno wysłać jej drugi raz.
                self.app.back_to_main()
                self.app.notify(
                    f"Faktura {self.draft.number} czeka na wynik z KSeF. "
                    "Aplikacja sprawdzi go sama przy następnym uruchomieniu.",
                    timeout=8,
                )
            else:
                self._set_draft(self.draft)
                self._reshow()

        self.app.push_screen(screen, done)

    @on(Button.Pressed, "#edit")
    def action_edit(self) -> None:
        from .editor import EditResult, EditScreen

        def edited(result: EditResult | None) -> None:
            if result is None:
                return
            self.draft_id = result.draft_id
            if result.draft is not None:
                self._set_draft(result.draft)
                self._reshow()
                return
            # Szkic porzucony albo zostawiony z błędami - podsumowanie byłoby nieaktualne.
            self.app.pop_screen()
            if result.discarded:
                self.app.notify("Szkic porzucony.", timeout=3)
            else:
                self.app.notify(draft_saved_text() + " Dokończ go, zanim wyślesz.", timeout=8)
            self.app.refresh_main()

        self.app.push_screen(EditScreen(self.draft, draft_id=self.draft_id), edited)

    def action_save_xml(self) -> None:
        self.app.export_file(
            f"{safe_name(self.draft.number)}_projekt.xml", self.xml, title="Projekt faktury (niewysłany)"
        )

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.app.pop_screen()
        self.app.notify(draft_saved_text(), timeout=5)
        self.app.refresh_main()

    def action_discard(self) -> None:
        if self.draft_id is not None:
            self.app.discard_draft(self.draft_id, then=self.app.pop_screen)


# ---------------------------------------------------------------------------- wysyłka

# Etapy wysyłki widoczne dla użytkownika i kroki klienta KSeF, które się na nie składają.
PHASES = [
    ("connect", "Łączenie z KSeF"),
    ("upload", "Przesyłanie faktury"),
    ("processing", "KSeF sprawdza fakturę"),
    ("upo", "Potwierdzenie odbioru (UPO)"),
]
PHASE_OF = {"auth": "connect", "session": "connect", "upload": "upload", "processing": "processing", "upo": "upo"}
TECHNICAL = {
    "auth": "uwierzytelnienie",
    "session": "otwarcie sesji interaktywnej",
    "upload": "przesłanie zaszyfrowanej faktury",
    "processing": "weryfikacja faktury",
    "upo": "pobranie UPO",
    "close": "zamknięcie sesji",
}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def render_steps(steps: list[tuple[str, str]], state: dict[str, str], current: str | None, frame: int) -> str:
    lines = []
    for key, label in steps:
        value = state.get(key)
        if value == "ok":
            lines.append(f"  [$text-success]✓[/] {label}")
        elif value == "error":
            lines.append(f"  [$text-error]✗[/] {label}")
        elif key == current:
            lines.append(f"  [$text-accent]{SPINNER[frame % len(SPINNER)]}[/] [b]{label}[/b]")
        elif value:
            lines.append(f"  [$text-warning]•[/] {label} [$foreground-muted]({escape(value)})[/]")
        else:
            lines.append(f"  [$foreground-muted]○ {label}[/]")
    return "\n".join(lines)


class SendScreen(Screen[str | None]):
    """Wysyłka z podglądem postępu. Zwraca numer KSeF przyjętej faktury albo None; stan końcowy w `outcome`."""

    HELP = (
        "Postęp wysyłki do KSeF. Gdy wynik jest nieznany, nie wysyłaj faktury ponownie - aplikacja sprawdzi go "
        "sama (albo naciśnij „Sprawdź status”). Historię wysyłek znajdziesz w Wysyłkach i UPO."
    )
    BINDINGS = [
        Binding("escape", "close", "Zamknij"),
        Binding("o", "open_verify", "Otwórz weryfikację KSeF"),
        Binding("d", "details", "Szczegóły techniczne"),
    ]

    app: KsefApp

    def __init__(self, draft: Draft, xml: bytes, *, draft_id: int | None = None) -> None:
        super().__init__()
        self.draft = draft
        self.xml = xml
        self.draft_id = draft_id
        self.state: dict[str, str] = {}
        self.current: str | None = None
        self.frame = 0
        self.finished = False
        self.outcome = "sending"  # zob. store.SENT_STATES
        self.ksef_number = ""
        self.url = ""  # link weryfikacyjny przyjętej faktury
        self.sent_id: int | None = None
        self.steps_log: list[str] = []

    @property
    def success(self) -> bool:
        return self.outcome == "accepted"

    def compose(self) -> ComposeResult:
        profile = self.app.profile
        draft = self.draft
        yield from topbar(f"[b]Wysyłanie faktury[/b]  {escape(draft.number)}", self.app)
        with Center(), Vertical(id="send-box", classes="dialog"):
            # Treść przewija się, przyciski zostają widoczne także w małym oknie terminala.
            with VerticalScroll(id="send-body"):
                yield Static(
                    f"Faktura [b]{escape(draft.number)}[/b] dla {escape(draft.buyer.nazwa)}\n"
                    f"[$foreground-muted]Brutto {fmt_amount(draft.gross)} {draft.currency} · "
                    f"{escape(profile.seller.nazwa)} · środowisko: {profile.env_label}[/]",
                    classes="dialog-text",
                )
                yield Static("", id="steps")
                yield Static("", id="result")
                yield Static("", id="details")
            with Horizontal(classes="dialog-buttons", id="result-buttons"):
                yield Button("Gotowe", id="done", variant="success")
                yield Button("Kopiuj numer KSeF", id="copy", variant="primary")
                yield Button("Kopiuj link", id="copy-link")
                yield Button("Sprawdź status", id="recheck", variant="warning")
                yield Button("← Popraw fakturę", id="fix", variant="primary")
                yield Button("← Wróć do podsumowania", id="back", variant="primary")
                yield Button("Wróć do faktur", id="to-list")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#details").display = False
        self._show()
        self._render_steps()
        self.set_interval(0.1, self._tick)
        self.send()

    def _tick(self) -> None:
        if self.current and not self.finished:
            self.frame += 1
            self._render_steps()

    def _render_steps(self) -> None:
        self.query_one("#steps", Static).update(render_steps(PHASES, self.state, self.current, self.frame))
        refs = []
        if self.sent_id is not None and (row := self.app.store.sent_entry(self.sent_id)):
            refs = [
                f"Wysyłka nr {row['id']} · stan: {SENT_STATES.get(row['status'], row['status'])}",
                f"Sesja KSeF: {row['session_ref'] or '—'}",
                f"Numer referencyjny faktury: {row['invoice_ref'] or '—'}",
            ]
        lines = [escape(line) for line in [*self.steps_log, *refs]]
        self.query_one("#details", Static).update("[$foreground-muted]" + "\n".join(lines) + "[/]")

    def _progress(self, step: str, state: str) -> None:
        self.steps_log.append(f"{TECHNICAL.get(step, step)}: {state}")
        phase = PHASE_OF.get(step)
        if phase is None:
            self._render_steps()
            return
        if state == "start":
            self.current = phase
        elif step != "auth":  # logowanie to dopiero połowa łączenia - kończy je otwarcie sesji
            self.state[phase] = state
            if self.current == phase:
                self.current = None
        self._render_steps()

    def _show(self, *buttons: str) -> None:
        for button in self.query("#result-buttons Button"):
            button.display = button.id in buttons
        if buttons:
            self.query_one(f"#{buttons[0]}", Button).focus()
            self.query_one("#send-body").scroll_end(animate=False)  # wynik jest pod listą kroków

    def _mark_current(self, state: str) -> None:
        if self.current:
            self.state[self.current] = state
            self.current = None

    @work(exclusive=True)
    async def send(self) -> None:
        store = self.app.store
        profile = self.app.profile
        draft = self.draft
        # Wpis w historii powstaje przed połączeniem: nawet zamknięcie aplikacji w trakcie nie zgubi wysyłki.
        self.sent_id = sent_id = store.record_sent(
            invoice_number=draft.number,
            status="sending",
            xml=self.xml,
            env=profile.env,
            context_id=profile.context_id,
            seller_nip=draft.seller.nip,
            invoice_kind=draft.kind,
            draft_id=self.draft_id,
            source="app",
        )
        self.app.active_sends.add(sent_id)

        def on_refs(**refs: str) -> None:
            if "invoice_ref" in refs:
                refs["status"] = "processing"  # faktura jest już w KSeF
            store.update_sent(sent_id, **refs)

        try:
            client = await self.app.get_client()
            if client is None:
                self._not_submitted("Nie połączono z KSeF - nie podano danych logowania.")
                return
            result = await client.send_invoice(self.xml, self._progress, on_refs)
        except NotSubmitted as exc:
            self._mark_current("error")
            self._not_submitted(exc.human())
        except OutcomeUnknown as exc:
            self._mark_current("error")
            store.update_sent(sent_id, status="unknown", session_ref=exc.session_ref)
            self._show_unresolved("unknown")
        except StillProcessing as exc:
            self._mark_current("error")
            store.update_sent(
                sent_id, status="processing", session_ref=exc.session_ref, invoice_ref=exc.invoice_ref or None
            )
            self._show_unresolved("processing")
        except InvoiceRejected as exc:
            self._mark_current("error")
            store.update_sent(sent_id, status="rejected", message=exc.human())
            self._show_rejected(exc)
        except KsefError as exc:
            # Nie wiadomo, na którym etapie - jeśli sesja była otwarta, faktura mogła dotrzeć do KSeF.
            self._mark_current("error")
            row = store.sent_entry(sent_id)
            if row and row["session_ref"]:
                store.update_sent(sent_id, status="unknown", message=exc.human())
                self._show_unresolved("unknown")
            else:
                self._not_submitted(exc.human())
        else:
            exported = finalize_accepted(
                store,
                profile,
                sent_id,
                result.ksef_number,
                acquired=result.acquisition_date,
                upo=result.upo_xml,
                session_ref=result.session_ref,
                invoice_ref=result.invoice_ref,
            )
            self._show_accepted(result.ksef_number, exported, bool(result.upo_xml))
        finally:
            self.app.active_sends.discard(sent_id)
            self.finished = True
            self.current = None
            if self.is_attached:
                with suppress(NoMatches):
                    self._render_steps()

    # ------------------------------------------------------------------ wyniki

    def _result(self, text: str) -> None:
        self.query_one("#result", Static).update(text)

    def _not_submitted(self, reason: str) -> None:
        self.outcome = "not_submitted"
        if self.sent_id is not None:
            self.app.store.update_sent(self.sent_id, status="not_submitted", message=reason)
        self._result(
            f"[b $text-error]Nie wysłano faktury.[/]\n{escape(reason)}\n\n"
            "Faktura nie trafiła do KSeF - możesz bezpiecznie spróbować ponownie."
        )
        self._show("back")

    def _show_unresolved(self, status: str, note: str = "") -> None:
        self.outcome = status
        if status == "processing":
            text = (
                "[b $text-warning]KSeF otrzymał fakturę i nadal ją przetwarza.[/]\n"
                "Nie wysyłaj jej ponownie. Sprawdź status za chwilę albo wróć do pracy - "
                "aplikacja sprawdzi go sama przy następnym uruchomieniu."
            )
        else:
            text = (
                "[b $text-warning]Nie można teraz potwierdzić wyniku wysyłki.[/]\n"
                "Połączenie przerwało się w trakcie przesyłania - KSeF mógł otrzymać fakturę. "
                "Nie wysyłaj jej ponownie: sprawdzimy istniejącą wysyłkę (teraz przyciskiem albo sami "
                "przy następnym uruchomieniu)."
            )
        if note:
            text += f"\n[$foreground-muted]{escape(note)}[/]"
        self._result(text)
        self._show("recheck", "to-list")

    def _show_rejected(self, exc: InvoiceRejected | None, message: str = "") -> None:
        self.outcome = "rejected"
        reason = exc.message if exc is not None else message
        if not reason.startswith("KSeF odrzucił"):
            reason = f"KSeF odrzucił fakturę: {reason}"
        headline, _, rest = reason.partition("\n")
        text = f"[b $text-error]{escape(headline)}[/]" + (f"\n{escape(rest)}" if rest else "")
        if exc is not None:
            text += "".join(f"\n• {escape(d)}" for d in exc.details)
            if exc.code == 440:
                original = exc.extensions.get("originalKsefNumber")
                text += escape(f"\n\nFaktura o tym numerze już jest w KSeF{f' ({original})' if original else ''}.")
        text += (
            "\n\n[$foreground-muted]Szkic i szczegóły wysyłki są zachowane - popraw fakturę i wyślij ją ponownie.[/]"
        )
        self._result(text)
        self._show("fix", "to-list")

    def _show_accepted(self, ksef_number: str, exported: Exported, has_upo: bool) -> None:
        profile = self.app.profile
        draft = self.draft
        self.outcome = "accepted"
        self.ksef_number = ksef_number
        self.current = None
        self.state.update(connect="ok", upload="ok", processing="ok")
        self.state["upo"] = "ok" if exported.upo_path or has_upo else "jeszcze niedostępne"
        self._render_steps()
        self.url = url = verify_url(profile.qr_url, draft.seller.nip, draft.issue_date, sha256_b64(self.xml))
        self.refresh_bindings()
        lines = [
            "[b $text-success]Faktura przyjęta przez KSeF ✓[/]",
            "",
            f"[$foreground-muted]Numer KSeF:[/] [b]{escape(ksef_number)}[/b]",
        ]
        if exported.error:
            lines.append(
                f"[$text-warning]Faktura została przyjęta, ale nie udało się zapisać pliku w "
                f"{escape(short_path(exported.folder))} ({escape(exported.error)}). "
                "XML i UPO zapiszesz z podglądu faktury (x, u).[/]"
            )
        elif exported.xml_path:
            saved = "XML i UPO" if exported.upo_path else "XML"
            lines.append(f"[$foreground-muted]{saved} zapisano w[/] {escape(short_path(exported.folder))}/")
        if not exported.upo_path and not has_upo:
            lines.append("[$text-warning]UPO nie jest jeszcze dostępne - pobierzesz je później w podglądzie (u).[/]")
        lines += ["", "[$foreground-muted]Link weryfikacyjny (kliknij albo naciśnij o, aby otworzyć):[/]", link(url)]
        self._result("\n".join(lines))
        self._show("done", "copy", "copy-link")

    # ---------------------------------------------------------------- przyciski

    @on(Button.Pressed, "#copy")
    def _copy(self) -> None:
        self.app.copy_text(self.ksef_number, "numer KSeF")

    @on(Button.Pressed, "#copy-link")
    def action_copy_link(self) -> None:
        if self.url:
            self.app.copy_text(self.url, "link weryfikacyjny")

    def action_open_verify(self) -> None:
        if self.url:
            self.app.action_open_link(self.url)

    @on(Button.Pressed, "#done")
    @on(Button.Pressed, "#fix")
    @on(Button.Pressed, "#back")
    @on(Button.Pressed, "#to-list")
    def _done(self) -> None:
        self.dismiss(self.ksef_number or None)

    @on(Button.Pressed, "#recheck")
    def _recheck_pressed(self) -> None:
        self.recheck()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("open_verify", "copy_link"):
            return bool(self.url)
        return True

    def action_details(self) -> None:
        details = self.query_one("#details")
        details.display = not details.display
        if details.display:
            self.query_one("#send-body").scroll_end(animate=False)

    @work(exclusive=True)
    async def recheck(self) -> None:
        store = self.app.store
        row = store.sent_entry(self.sent_id) if self.sent_id is not None else None
        if row is None:
            return
        client = await self.app.get_client()
        if client is None:
            return
        self._show()
        self._result("[$foreground-muted]Sprawdzam status w KSeF…[/]")
        try:
            result = await reconcile(client, store, self.app.profile, row, timeout=30)
        except (RateLimitError, AuthError) as exc:
            self._result(f"[b $text-warning]{escape(exc.human())}[/]\nSpróbuj ponownie za chwilę.")
            self._show("recheck", "to-list")
            return
        if result.status == "accepted":
            self._show_accepted(result.ksef_number, result.exported or Exported(self.app.profile.data_dir), False)
        elif result.status == "rejected":
            self._show_rejected(None, result.message)
        elif result.status == "not_submitted":
            self._not_submitted(result.message)
        else:
            self._show_unresolved(result.status)
            if result.message:
                self._result(
                    str(self.query_one("#result", Static).content) + f"\n[$foreground-muted]{escape(result.message)}[/]"
                )

    def action_close(self) -> None:
        if not self.finished:
            self.notify("Poczekaj na zakończenie wysyłki.", severity="warning", timeout=3)
            return
        self._done()

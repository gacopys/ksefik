"""Edycja faktury: dane faktury, nabywca, pozycje, płatność, adnotacje i korekta."""

from __future__ import annotations

import re
import sqlite3
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.markup import escape
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.validation import Function
from textual.widgets import Button, Checkbox, Collapsible, DataTable, Footer, Input, Label, Select, Static
from textual.widgets.select import InvalidSelectValueError

from .. import registry
from ..codes import CORRECTION_TYPES, EU_COUNTRIES, GTU_CODES, MARGIN_PROCEDURES, PAYMENT_FORMS, VAT_RATES
from ..config import os_error_text
from ..fa_parser import parse_draft
from ..invoice import (
    OTHER_PAYMENT,
    CorrectedInvoice,
    Draft,
    Line,
    Payment,
    accounts_for,
    due_days,
    next_number,
    rate_totals,
)
from ..money import NBSP, fmt_account, fmt_amount, fmt_date, fmt_qty, nip_valid, parse_amount, parse_date
from ..profile import BankAccount, CatalogItem, Contractor, Party, append_block, render_contractor, render_item
from .common import ChoiceScreen, ConfirmScreen, draft_saved_text, topbar
from .widgets import amount_preview

if TYPE_CHECKING:
    from .app import KsefApp

ID_TYPES = [("NIP", "nip"), ("Numer VAT UE", "vat_ue"), ("Inny identyfikator", "nr_id"), ("Bez identyfikatora", "brak")]
RATE_OPTIONS = [(rate.label, code) for code, rate in VAT_RATES.items()]
NO_ACCOUNT = "__none__"


def _plain_amount(value: Decimal) -> str:
    if value != value.quantize(Decimal("0.01")):
        return fmt_qty(value)
    return fmt_amount(value).replace(NBSP, " ")


def _date_valid(value: str) -> bool:
    return parse_date(value) is not None


def _not_empty(what: str) -> Function:
    return Function(lambda v: bool(v.strip()), f"{what} nie może być puste")


DATE_CHECK = Function(_date_valid, "Data w formacie DD.MM.RRRR")
OPTIONAL_DATE = Function(lambda v: not v.strip() or _date_valid(v), "Data w formacie DD.MM.RRRR")
POSITIVE = Function(lambda v: (parse_amount(v) or 0) > 0, "Wartość większa od zera")
NON_NEGATIVE = Function(lambda v: parse_amount(v) is not None and parse_amount(v) >= 0, "Wartość nie może być ujemna")
EMAIL = Function(
    lambda v: not v.strip() or re.fullmatch(r"[^@\s]+@[^@\s]+", v.strip()) is not None, "Niepoprawny e-mail"
)


def form_row(label: str | None, *widgets: Any, row_id: str | None = None, classes: str = "") -> Horizontal:
    """Wiersz formularza: etykieta w stałej kolumnie i pola; bez etykiety (None) pola zaczynają się od lewej."""
    children = ([Label(label, classes="form-label")] if label is not None else []) + list(widgets)
    return Horizontal(*children, classes=f"form-row {classes}".strip(), id=row_id)


# ------------------------------------------------------------------- pozycja faktury


class LineScreen(ModalScreen[Line | None]):
    """Dodawanie i edycja jednej pozycji (towaru lub usługi)."""

    BINDINGS = [Binding("escape", "cancel", "Anuluj"), Binding("ctrl+s", "save", "Zapisz pozycję")]

    app: KsefApp

    def __init__(self, line: Line | None, *, correction: bool = False, currency: str = "PLN") -> None:
        super().__init__()
        self.line = line or Line(name="", unit="szt.")
        self.new = line is None
        self.correction = correction
        self.currency = currency

    def compose(self) -> ComposeResult:
        line = self.line
        items = self.app.profile.items
        with VerticalScroll(classes="dialog line-dialog"):
            yield Static("[b]Nowa pozycja[/b]" if self.new else "[b]Edycja pozycji[/b]", classes="dialog-title")
            if items:
                catalog = [(item.nazwa, item.nazwa) for item in items]
                yield form_row(
                    "Z katalogu", Select(catalog, prompt="wybierz, aby uzupełnić", id="catalog", compact=True)
                )
            yield form_row("Nazwa", Input(line.name, id="name", compact=True, validators=[_not_empty("Nazwa")]))
            check = NON_NEGATIVE if self.correction else POSITIVE
            yield form_row("Ilość", Input(fmt_qty(line.quantity), id="quantity", compact=True, validators=[check]))
            yield form_row("Jednostka", Input(line.unit, id="unit", compact=True, placeholder="szt., usł., godz."))
            price = _plain_amount(line.unit_price)
            yield form_row(
                f"Cena netto ({self.currency})", Input(price, id="price", compact=True, validators=[NON_NEGATIVE])
            )
            yield form_row(
                "Stawka VAT", Select(RATE_OPTIONS, value=line.vat_rate, allow_blank=False, id="rate", compact=True)
            )
            gtu = [(f"{code} – {label}", code) for code, label in GTU_CODES.items()]
            # Kody potrzebne tylko niektórym - zwinięte, chyba że któryś jest już ustawiony.
            used = bool(line.gtu or line.pkwiu or line.cn or line.index or line.annex15)
            with Collapsible(title="Kody: GTU, PKWiU, CN, indeks, zał. 15", collapsed=not used, id="codes"):
                yield form_row("GTU", Select(gtu, value=line.gtu or Select.NULL, prompt="brak", id="gtu", compact=True))
                yield form_row("PKWiU", Input(line.pkwiu, id="pkwiu", compact=True, placeholder="opcjonalnie"))
                yield form_row("CN", Input(line.cn, id="cn", compact=True, placeholder="opcjonalnie"))
                yield form_row("Indeks", Input(line.index, id="index", compact=True, placeholder="kod wewnętrzny"))
                yield form_row(
                    "",
                    Checkbox("Towar/usługa z zał. 15 (podzielona płatność)", line.annex15, id="annex15", compact=True),
                )
            with Vertical(classes="dialog-footer"):
                yield Static("", id="line-preview")
                with Horizontal(classes="dialog-buttons"):
                    yield Button("Zapisz w katalogu", id="to-catalog")
                    yield Button("Anuluj", id="cancel")
                    yield Button("✓ Zapisz pozycję", variant="primary", id="save")

    def on_mount(self) -> None:
        self._preview()
        self.query_one("#name" if self.new else "#price", Input).focus()

    @on(Select.Changed, "#catalog")
    def _from_catalog(self, event: Select.Changed) -> None:
        item = self.app.profile.item(str(event.value)) if event.value is not Select.NULL else None
        if item is None:
            return
        self.query_one("#name", Input).value = item.nazwa
        self.query_one("#unit", Input).value = item.jednostka
        self.query_one("#rate", Select).value = item.stawka_vat
        self.query_one("#gtu", Select).value = item.gtu or Select.NULL
        self.query_one("#pkwiu", Input).value = item.pkwiu
        self.query_one("#cn", Input).value = item.cn
        self.query_one("#index", Input).value = item.indeks
        self.query_one("#annex15", Checkbox).value = item.zal15
        if item.gtu or item.pkwiu or item.cn or item.indeks or item.zal15:
            self.query_one("#codes", Collapsible).collapsed = False  # pokaż, co przyszło z katalogu
        if item.cena_netto is not None:
            self.query_one("#price", Input).value = _plain_amount(item.cena_netto)

    @on(Input.Changed, "#quantity")
    @on(Input.Changed, "#price")
    @on(Select.Changed, "#rate")
    def _preview(self) -> None:
        qty = parse_amount(self.query_one("#quantity", Input).value)
        price = parse_amount(self.query_one("#price", Input).value)
        rate = str(self.query_one("#rate", Select).value)
        net = (qty * price) if qty is not None and price is not None else None
        self.query_one("#line-preview", Static).update(amount_preview(net, rate, self.currency))

    def _collect(self) -> Line | None:
        inputs = list(self.query(Input))
        for widget in inputs:
            widget.validate(widget.value)
        invalid = [w for w in inputs if not w.is_valid]
        if invalid:
            self.notify("Popraw zaznaczone pola pozycji.", severity="error", timeout=3)
            invalid[0].focus()
            return None
        value = lambda wid: self.query_one(f"#{wid}", Input).value.strip()  # noqa: E731
        gtu = self.query_one("#gtu", Select).value
        return Line(
            name=value("name"),
            quantity=parse_amount(value("quantity")) or Decimal(0),
            unit=value("unit") or "szt.",
            unit_price=parse_amount(value("price")) or Decimal(0),
            vat_rate=str(self.query_one("#rate", Select).value),
            gtu="" if gtu is Select.NULL else str(gtu),
            pkwiu=value("pkwiu"),
            cn=value("cn"),
            index=value("index"),
            annex15=self.query_one("#annex15", Checkbox).value,
        )

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        if (line := self._collect()) is not None:
            self.dismiss(line)

    @on(Button.Pressed, "#to-catalog")
    def _to_catalog(self) -> None:
        line = self._collect()
        if line is None:
            return
        profile = self.app.profile
        if profile.item(line.name):
            self.notify("Pozycja o tej nazwie już jest w katalogu profilu.", severity="warning", timeout=4)
            return
        item = CatalogItem(
            nazwa=line.name,
            jednostka=line.unit,
            stawka_vat=line.vat_rate,
            cena_netto=line.unit_price or None,
            gtu=line.gtu,
            pkwiu=line.pkwiu,
            cn=line.cn,
            indeks=line.index,
            zal15=line.annex15,
        )
        try:
            append_block(profile.path, render_item(item))
        except OSError as exc:
            self.notify(f"Nie udało się zapisać profilu: {os_error_text(exc)}", severity="error", timeout=8)
            return
        self.app.reload_profile(quiet=True)
        self.notify(f"Dodano „{line.name}” do katalogu pozycji w profilu.", timeout=4)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ------------------------------------------------------------------------- edycja


@dataclass
class EditResult:
    """Wynik edycji: poprawny projekt (None - szkic niedokończony), szkic w bazie i co dalej."""

    draft: Draft | None
    draft_id: int | None
    review: bool = False  # użytkownik chce sprawdzić fakturę (F5); inaczej zamknął edycję
    discarded: bool = False


class EditScreen(Screen[EditResult]):
    """Edycja faktury. Wszystko, co wpisano, zapisuje się samo jako szkic - także niedokończone."""

    HELP = (
        "Edycja faktury. Wszystko, co wpiszesz, zapisuje się samo jako szkic. „Sprawdź fakturę” (F5) pokazuje "
        "podsumowanie przed wysyłką - z edytora nic nie trafia do KSeF."
    )

    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (140, "-wide")]
    BINDINGS = [
        # Sprawdzenie faktury nigdy jej nie wysyła - wysyłka jest osobnym, jawnym krokiem w podsumowaniu.
        Binding("f5", "save", "Sprawdź fakturę"),
        Binding("ctrl+s", "save_draft", "Zapisz szkic"),
        Binding("ctrl+n", "add_line", "Dodaj pozycję"),
        Binding("ctrl+t", "save_contractor", "Zapisz kontrahenta"),
        Binding("escape", "close", "Zamknij"),
    ]
    AUTOSAVE_DELAY = 1.0

    app: KsefApp

    def __init__(
        self,
        draft: Draft,
        *,
        draft_id: int | None = None,
        fields: dict[str, Any] | None = None,
        source_ksef: str | None = None,
    ) -> None:
        super().__init__()
        self.draft = draft
        self.draft_id = draft_id
        self.source_ksef = source_ksef
        self._restore = dict(fields or {})
        self._touched: set[str] = set(fields or {})  # w przywróconym szkicu wszystko wpisał użytkownik
        self._baseline: Any = None  # stan pól tuż po otwarciu - szkic zapisujemy dopiero po zmianie
        self._save_timer: Timer | None = None
        self.save_error = ""
        self.saved_at: datetime | None = None
        self.lines: list[Line] = list(draft.lines)
        profile = self.app.profile
        self.issued = self.app.store.reserved_numbers() + self.app.store.draft_numbers(exclude=draft_id)
        contractor = profile.contractor(draft.buyer)
        self.due_days = due_days(profile, contractor)
        # Dopóki termin to "data wystawienia + N dni", przesuwamy go razem z datą wystawienia.
        due = draft.payment.due_date
        self._due_follows_issue = draft.kind != "KOR" and (
            due is None or due == draft.issue_date + timedelta(days=self.due_days)
        )
        # Dopóki numer jest automatyczny, podąża za miesiącem/rokiem daty wystawienia.
        self._pattern = profile.invoice.numeracja_korekt if draft.kind == "KOR" else profile.invoice.numeracja
        self._auto_number = draft.number == next_number(self._pattern, self.issued, draft.issue_date)
        self.buyers: dict[str, Party] = {c.party.key: c.party for c in profile.contractors}
        for xml in self.app.store.recent_buyer_invoices():
            try:
                buyer = parse_draft(xml).buyer
            except (ValueError, TypeError):
                continue
            self.buyers.setdefault(buyer.key, buyer)
        self.accounts: list[BankAccount] = list(profile.accounts)
        for account in draft.payment.accounts:
            if all(a.number != account.number for a in self.accounts):
                self.accounts.append(account)
        self._labels: dict[str, str] = {}

    # ----------------------------------------------------------------- układ

    def compose(self) -> ComposeResult:
        d = self.draft
        correcting = d.kind == "KOR"
        yield from topbar("[b]Edycja korekty[/b]" if correcting else "[b]Edycja faktury[/b]", self.app)
        with VerticalScroll(id="form"), Grid(id="form-grid"):
            if correcting:
                # Dwie kolumny: faktura korygowana | faktura, nabywca | płatność, potem pozycje na całą szerokość.
                yield from self._correction_section()
            yield from self._invoice_section()
            yield from self._buyer_section()
            if correcting:
                yield from self._payment_section()
            with Vertical(classes="form-section wide-section") as section:
                section.border_title = "Pozycje" + (" (stan po korekcie)" if correcting else "")
                yield DataTable(cursor_type="row", zebra_stripes=True, id="lines")
                with Horizontal(classes="lines-buttons"):
                    yield Button("＋ Dodaj", id="line-add", compact=True)
                    yield Button("✎ Edytuj", id="line-edit", compact=True)
                    yield Button("✕ Usuń", id="line-remove", compact=True)
                    yield Button("↑", id="line-up", compact=True)
                    yield Button("↓", id="line-down", compact=True)
                    yield Static(
                        "[$foreground-muted]Enter - edycja, Del - usuń, Ctrl+N - nowa pozycja[/]", classes="lines-hint"
                    )
                yield Static("", id="totals")
            if not correcting:
                yield from self._payment_section()
            yield from self._annotations_section()
            yield from self._extra_section()
        with Horizontal(id="form-buttons"):
            yield Button("Sprawdź fakturę →", variant="primary", id="save")
            yield Button("Zamknij", id="close")
            yield Static("", id="draft-state")
            yield Button("Porzuć szkic", variant="error", id="discard")
        yield Footer()

    def _input(self, field_id: str, label: str, value: str, *validators: Function, hint: bool = False, **kwargs):
        widget = Input(value, id=field_id, compact=True, validators=list(validators), **kwargs)
        self._labels[field_id] = label
        children = [widget] + ([Static("", id=f"{field_id}-hint", classes="form-hint")] if hint else [])
        return form_row(label, *children, row_id=f"row-{field_id}")

    def _correction_section(self) -> ComposeResult:
        corr = self.draft.correction or CorrectedInvoice("", self.draft.issue_date)
        with Vertical(classes="form-section") as section:
            section.border_title = "Faktura korygowana"
            yield self._input("corr_number", "Numer faktury", corr.number, _not_empty("Numer"))
            yield self._input("corr_date", "Data wystawienia", fmt_date(corr.issue_date), DATE_CHECK)
            yield self._input("corr_ksef", "Numer KSeF", corr.ksef_number, placeholder="puste = spoza KSeF")
            yield self._input("corr_reason", "Przyczyna korekty", corr.reason, placeholder="np. zmiana ceny")
            effects = [(label, code) for code, label in CORRECTION_TYPES.items()]
            yield form_row(
                "Skutek korekty",
                Select(effects, value=corr.effect or Select.NULL, prompt="nie podano", id="corr_effect", compact=True),
            )
            before = rate_totals(corr.lines)
            gross = sum((t.gross for t in before), Decimal(0))
            yield Static(
                f"[$foreground-muted]Stan przed korektą: {len(corr.lines)} poz., brutto {fmt_amount(gross)} "
                f"{self.draft.currency}. Pozycje poniżej to stan po korekcie - zmień ilości lub ceny; "
                "pozycję usuniętą z faktury ustaw na ilość 0.[/]",
                classes="section-note",
            )

    def _invoice_section(self) -> ComposeResult:
        d = self.draft
        with Vertical(classes="form-section") as section:
            section.border_title = "Faktura"
            yield self._input("number", "Numer faktury", d.number, _not_empty("Numer"))
            yield self._input("issue_date", "Data wystawienia", fmt_date(d.issue_date), DATE_CHECK)
            mode = "period" if d.period else "date"
            yield form_row(
                "Sprzedaż",
                Select(
                    [("data wykonania usługi / dostawy", "date"), ("okres (od - do)", "period")],
                    value=mode,
                    allow_blank=False,
                    id="sale_mode",
                    compact=True,
                ),
            )
            sale = fmt_date(d.sale_date) if d.sale_date else ""
            yield self._input("sale_date", "Data wykonania", sale, OPTIONAL_DATE)
            start, end = d.period or (None, None)
            yield self._input("period_from", "Okres od", fmt_date(start), OPTIONAL_DATE)
            yield self._input("period_to", "Okres do", fmt_date(end), OPTIONAL_DATE)
            yield self._input("place", "Miejsce wystawienia", d.place, placeholder="opcjonalnie")
            yield self._input(
                "currency",
                "Waluta",
                d.currency,
                Function(lambda v: re.fullmatch(r"[A-Za-z]{3}", v.strip()) is not None, "Kod waluty, np. EUR"),
                max_length=3,
            )
            rate = format(d.exchange_rate.normalize(), "f").replace(".", ",") if d.exchange_rate else ""
            yield self._input(
                "exchange_rate",
                "Kurs (PLN za 1 jedn.)",
                rate,
                Function(lambda v: not v.strip() or (parse_amount(v) or 0) > 0, "Kurs większy od zera"),
                placeholder="np. 4,2512",
            )
            yield self._input("exchange_info", "Źródło kursu", d.exchange_rate_info, placeholder="np. tabela NBP …")

    def _buyer_section(self) -> ComposeResult:
        b = self.draft.buyer
        id_type = "nip" if b.nip else "vat_ue" if b.vat_ue else "nr_id" if b.nr_id else "brak" if b.brak_id else "nip"
        with Vertical(classes="form-section") as section:
            section.border_title = "Nabywca"
            if self.buyers:
                options = [(f"{p.nazwa[:40]}  ({p.identifier or 'bez id.'})", k) for k, p in self.buyers.items()]
                yield form_row("Z listy", Select(options, prompt="wybierz kontrahenta", id="buyer_pick", compact=True))
            yield form_row(
                "Identyfikator",
                Select(ID_TYPES, value=id_type, allow_blank=False, id="buyer_id_type", compact=True),
                Input(b.nip or b.vat_ue or b.nr_id, id="buyer_id", compact=True, classes="id-input"),
                Button("Pobierz dane", id="buyer_lookup", compact=True, tooltip="Nazwa i adres z Białej listy MF"),
            )
            yield self._input("buyer_name", "Nazwa", b.nazwa, _not_empty("Pole"))
            yield self._input("buyer_l1", "Ulica i numer", b.adres_l1, _not_empty("Pole"))
            yield self._input("buyer_l2", "Kod i miejscowość", b.adres_l2)
            yield self._input(
                "buyer_country",
                "Kraj (kod)",
                b.kraj or "PL",
                Function(lambda v: re.fullmatch(r"[A-Za-z]{2}", v.strip()) is not None, "Dwuliterowy kod kraju"),
                max_length=2,
            )
            yield self._input("buyer_email", "E-mail", b.email, EMAIL, placeholder="opcjonalnie")
            yield form_row("", Checkbox("Jednostka podrzędna JST", b.jst, id="buyer_jst", compact=True))
            yield form_row("", Checkbox("Członek grupy VAT", b.gv, id="buyer_gv", compact=True))

    def _payment_section(self) -> ComposeResult:
        p = self.draft.payment
        with Vertical(classes="form-section") as section:
            section.border_title = "Płatność"
            forms = [(label, code) for code, label in PAYMENT_FORMS.items()] + [("Inna", OTHER_PAYMENT)]
            yield form_row(
                "Forma płatności",
                Select(forms, value=p.form or Select.NULL, prompt="nie podano", id="pay_form", compact=True),
            )
            yield self._input("pay_other", "Opis formy", p.other_description, placeholder="np. kompensata")
            due = fmt_date(p.due_date) if p.due_date else ""
            yield self._input("due_date", "Termin płatności", due, OPTIONAL_DATE, hint=True)
            yield form_row("", Checkbox("Zapłacono", p.paid, id="paid", compact=True))
            paid_date = fmt_date(p.paid_date) if p.paid_date else ""
            yield self._input("paid_date", "Data zapłaty", paid_date, OPTIONAL_DATE)
            options = [(self._account_label(a), a.number) for a in self.accounts] + [("bez rachunku", NO_ACCOUNT)]
            current = p.accounts[0].number if p.accounts else NO_ACCOUNT
            yield form_row("Rachunek", Select(options, value=current, allow_blank=False, id="account", compact=True))

    @staticmethod
    def _account_label(account: BankAccount) -> str:
        extra = " · ".join(x for x in (account.waluta, account.bank, account.opis) if x)
        return fmt_account(account.number) + (f"  ({extra})" if extra else "")

    def _annotations_section(self) -> ComposeResult:
        d = self.draft
        with Vertical(classes="form-section") as section:
            section.border_title = "Adnotacje"
            used = d.split_payment or d.reverse_charge or d.cash_method or d.self_billing or d.related
            with Collapsible(
                title="Oznaczenia: MPP, odwrotne obciążenie, metoda kasowa, samofakturowanie, TP",
                collapsed=not used,
                id="marks",
            ):
                yield form_row(
                    None, Checkbox("Mechanizm podzielonej płatności", d.split_payment, id="mpp", compact=True)
                )
                yield form_row(None, Checkbox("Odwrotne obciążenie", d.reverse_charge, id="reverse", compact=True))
                yield form_row(None, Checkbox("Metoda kasowa", d.cash_method, id="cash", compact=True))
                yield form_row(None, Checkbox("Samofakturowanie", d.self_billing, id="self_billing", compact=True))
                yield form_row(None, Checkbox("Powiązania z nabywcą (TP)", d.related, id="related", compact=True))
            yield self._input("exemption", "Podstawa zwolnienia", d.exemption_basis, placeholder="art. 113 ust. 1…")
            margins = [(label, key) for key, (_, label) in MARGIN_PROCEDURES.items()]
            yield form_row(
                "Procedura marży",
                Select(margins, value=d.margin_procedure or Select.NULL, prompt="wybierz", id="margin", compact=True),
                row_id="row-margin",
            )

    def _extra_section(self) -> ComposeResult:
        d = self.draft
        with Vertical(classes="form-section") as section:
            section.border_title = "Dodatkowe informacje"
            yield self._input("notes", "Uwagi", d.notes, max_length=256, placeholder="pokazywane na fakturze")
            yield self._input("order_number", "Numer zamówienia", d.order_number)
            yield self._input("contract_number", "Numer umowy", d.contract_number)
            yield self._input("footer", "Stopka", d.footer)
            yield Static(self._third_parties_text(), id="third-parties")

    def on_mount(self) -> None:
        table = self.query_one("#lines", DataTable)
        table.add_column("Lp.", width=3)
        table.add_column("Nazwa towaru lub usługi")
        table.add_column(Text("Ilość", justify="right"))
        table.add_column("J.m.")
        table.add_column(Text("Cena netto", justify="right"))
        table.add_column(Text("VAT", justify="right"))
        table.add_column(Text("Wartość netto", justify="right"))
        self._apply_restore()
        self._refresh_lines()
        self._update_visibility()
        self._update_due_hint()
        self._update_draft_state()
        if not self.query_one("#buyer_name", Input).value.strip():
            self.query_one("#buyer_id", Input).focus()
        elif self.lines and not self.lines[0].name:
            self.call_after_refresh(self.action_edit_line)
        else:
            table.focus()
        # Zdarzenia z inicjalizacji pól to jeszcze nie zmiany użytkownika.
        self.call_after_refresh(self._mark_baseline)

    # ---------------------------------------------------------------- szkic

    def _raw_fields(self) -> dict[str, Any]:
        """Dokładnie to, co jest teraz w polach edytora (także niepoprawne, w trakcie pisania)."""
        values: dict[str, Any] = {}
        for widget in self.query("#form Input, #form Select, #form Checkbox"):
            if not widget.id or widget.id == "buyer_pick":
                continue
            value = widget.value
            values[widget.id] = None if value is Select.NULL else value
        return values

    def _apply_restore(self) -> None:
        if not self._restore:
            return
        with self.prevent(Input.Changed, Select.Changed, Checkbox.Changed):
            for widget_id, value in self._restore.items():
                try:
                    widget = self.query_one(f"#{widget_id}")
                except NoMatches:
                    continue
                if isinstance(widget, Select):
                    with suppress(InvalidSelectValueError):  # np. rachunek usunięty z profilu
                        widget.value = Select.NULL if value is None else value
                elif isinstance(widget, Input | Checkbox):
                    widget.value = value
        # Numer i termin wpisane ręcznie nie mogą się potem zmieniać same.
        self._auto_number = self._restore.get("number") == self.draft.number and self._auto_number
        self._restore = {}

    def _mark_baseline(self) -> None:
        self._baseline = self._state()

    def _state(self) -> tuple[Any, ...]:
        return (self._raw_fields(), tuple(self.lines))

    @property
    def dirty(self) -> bool:
        return self._baseline is not None and self._state() != self._baseline

    def _schedule_save(self) -> None:
        if self._baseline is None:
            return
        if self._save_timer:
            self._save_timer.stop()
        self._save_timer = self.set_timer(self.AUTOSAVE_DELAY, self._autosave)

    def _autosave(self) -> None:
        if self.dirty or (self.draft_id is not None and self.save_error):
            self.save_draft()

    def save_draft(self) -> bool:
        """Zapisuje szkic w bazie. Zwraca False, gdy zapis się nie udał (wpisane dane zostają w oknie)."""
        if self._save_timer:
            self._save_timer.stop()
            self._save_timer = None
        draft = self._collect(quiet=True) or self.draft.with_changes(lines=tuple(self.lines))
        try:
            self.draft_id = self.app.save_draft(
                draft, self.draft_id, fields=self._raw_fields(), source_ksef=self.source_ksef
            )
        except sqlite3.Error as exc:
            self.save_error = str(exc)
            self._update_draft_state()
            return False
        self.save_error = ""
        self.saved_at = datetime.now()
        self._baseline = self._state()
        self._update_draft_state()
        return True

    def _update_draft_state(self) -> None:
        if self.save_error:
            text = "[$text-error]Nie udało się zapisać szkicu[/]"
        elif self.saved_at:
            text = f"[$foreground-muted]Szkic zapisany {self.saved_at:%H:%M:%S}[/]"
        else:
            text = "[$foreground-muted]Zmiany zapisują się same jako szkic[/]"
        self.query_one("#draft-state", Static).update(text)
        self.query_one("#discard", Button).display = self.draft_id is not None

    def action_save_draft(self) -> None:
        if self.save_draft():
            self.notify(draft_saved_text(self.saved_at), timeout=5)
        else:
            self.notify(
                f"Nie udało się zapisać szkicu: {self.save_error}\nWpisane dane nadal są w tym oknie.",
                severity="error",
                timeout=10,
            )

    def flush(self) -> bool:
        """Zapisuje niezapisane zmiany (np. przed zamknięciem aplikacji)."""
        return self.save_draft() if self.dirty else not self.save_error

    @on(Input.Changed, "#form Input")
    @on(Select.Changed, "#form Select")
    @on(Checkbox.Changed, "#form Checkbox")
    def _something_changed(self, event: Input.Changed | Select.Changed | Checkbox.Changed) -> None:
        control = event.control
        # Zmiana z klawiatury lub myszy (pole ma fokus) - takich wartości zmiana nabywcy już nie nadpisze.
        if control is not None and control.id and (control.has_focus or control.has_focus_within):
            self._touched.add(control.id)
        self._schedule_save()

    # ---------------------------------------------------------------- pozycje

    def _refresh_lines(self, select: int | None = None) -> None:
        table = self.query_one("#lines", DataTable)
        row = table.cursor_row if select is None else select
        table.clear()
        for n, line in enumerate(self.lines, 1):
            table.add_row(
                str(n),
                line.name or Text("(bez nazwy)", style="italic dim"),
                Text(fmt_qty(line.quantity), justify="right"),
                line.unit,
                Text(fmt_amount(line.unit_price), justify="right"),
                Text(VAT_RATES[line.vat_rate].short, justify="right"),
                Text(fmt_amount(line.net), justify="right"),
            )
        if self.lines:
            table.move_cursor(row=max(0, min(row, len(self.lines) - 1)))
        self._update_totals()
        self._update_visibility()
        self._schedule_save()

    def _update_totals(self) -> None:
        currency = self.query_one("#currency", Input).value.strip().upper() or "PLN"
        totals = rate_totals(self.lines)
        if not totals:
            self.query_one("#totals", Static).update("[$foreground-muted]Dodaj co najmniej jedną pozycję (Ctrl+N).[/]")
            return
        parts = [
            f"{t.rate.label}: netto {fmt_amount(t.net)}" + (f", VAT {fmt_amount(t.vat)}" if t.rate.percent else "")
            for t in totals
        ]
        gross = sum((t.gross for t in totals), Decimal(0))
        text = "  ·  ".join(parts) + f"\n[b]Razem brutto: {fmt_amount(gross)} {escape(currency)}[/b]"
        if self.draft.kind == "KOR" and self.draft.correction:
            before = sum((t.gross for t in rate_totals(self.draft.correction.lines)), Decimal(0))
            diff = gross - before
            text += f"   [$foreground-muted]różnica po korekcie:[/] [b]{'+' if diff > 0 else ''}{fmt_amount(diff)}[/b]"
        self.query_one("#totals", Static).update(text)

    def _currency(self) -> str:
        return self.query_one("#currency", Input).value.strip().upper() or "PLN"

    def action_add_line(self) -> None:
        def added(line: Line | None) -> None:
            if line is not None:
                self.lines.append(line)
                self._refresh_lines(select=len(self.lines) - 1)
                self._auto_annotations()
                self.query_one("#lines", DataTable).focus()

        correcting = self.draft.kind == "KOR"
        self.app.push_screen(LineScreen(None, correction=correcting, currency=self._currency()), added)

    def action_edit_line(self) -> None:
        if not self.lines:
            self.action_add_line()
            return
        index = self.query_one("#lines", DataTable).cursor_row

        def edited(line: Line | None) -> None:
            if line is not None:
                self.lines[index] = line
                self._refresh_lines(select=index)
                self._auto_annotations()
            self.query_one("#lines", DataTable).focus()

        correcting = self.draft.kind == "KOR"
        self.app.push_screen(LineScreen(self.lines[index], correction=correcting, currency=self._currency()), edited)

    def action_remove_line(self) -> None:
        if not self.lines:
            return
        index = self.query_one("#lines", DataTable).cursor_row
        corr = self.draft.correction
        if self.draft.kind == "KOR" and corr and index < len(corr.lines):
            self.lines[index] = replace(self.lines[index], quantity=Decimal(0))
            self.notify("W korekcie pozycję z faktury pierwotnej zeruje się (ilość 0) zamiast usuwać.", timeout=5)
        else:
            del self.lines[index]
        self._refresh_lines(select=index)

    def _move_line(self, step: int) -> None:
        index = self.query_one("#lines", DataTable).cursor_row
        target = index + step
        if self.draft.kind == "KOR":
            self.notify("W korekcie kolejność pozycji odpowiada fakturze pierwotnej.", severity="warning", timeout=4)
            return
        if 0 <= target < len(self.lines):
            self.lines[index], self.lines[target] = self.lines[target], self.lines[index]
            self._refresh_lines(select=target)

    @on(Button.Pressed, "#line-add")
    def _add_pressed(self) -> None:
        self.action_add_line()

    @on(Button.Pressed, "#line-edit")
    @on(DataTable.RowSelected, "#lines")
    def _edit_pressed(self) -> None:
        self.action_edit_line()

    @on(Button.Pressed, "#line-remove")
    def _remove_pressed(self) -> None:
        self.action_remove_line()

    @on(Button.Pressed, "#line-up")
    def _up_pressed(self) -> None:
        self._move_line(-1)

    @on(Button.Pressed, "#line-down")
    def _down_pressed(self) -> None:
        self._move_line(1)

    def on_key(self, event) -> None:
        if event.key == "delete" and self.query_one("#lines", DataTable).has_focus:
            event.stop()
            self.action_remove_line()

    def _auto_annotations(self) -> None:
        """Odwrotne obciążenie zaznacza się samo, gdy pojawia się stawka „oo” lub „np II”."""
        codes = {line.vat_rate for line in self.lines}
        if codes & {"oo", "np II"}:
            self.query_one("#reverse", Checkbox).value = True
        gross = sum((t.gross for t in rate_totals(self.lines)), Decimal(0))
        annex15 = any(line.annex15 for line in self.lines) and gross > 15000 and self._currency() == "PLN"
        if annex15 and not self.query_one("#mpp", Checkbox).value:
            self.query_one("#mpp", Checkbox).value = True
            self.notify("Zaznaczono mechanizm podzielonej płatności (pozycje z zał. 15 powyżej 15 000 zł).")

    # -------------------------------------------------------------- reakcje

    def _update_visibility(self) -> None:
        if not self.is_mounted:
            return
        mode = self.query_one("#sale_mode", Select).value
        self.query_one("#row-sale_date").display = mode == "date"
        self.query_one("#row-period_from").display = mode == "period"
        self.query_one("#row-period_to").display = mode == "period"
        foreign = self._currency() != "PLN"
        self.query_one("#row-exchange_rate").display = foreign
        self.query_one("#row-exchange_info").display = foreign
        form = self.query_one("#pay_form", Select).value
        self.query_one("#row-pay_other").display = form == OTHER_PAYMENT
        paid = self.query_one("#paid", Checkbox).value
        self.query_one("#row-paid_date").display = paid
        self.query_one("#row-due_date").display = not paid
        codes = {line.vat_rate for line in self.lines}
        self.query_one("#row-exemption").display = "zw" in codes or bool(self.query_one("#exemption", Input).value)
        self.query_one("#row-margin").display = "marza" in codes
        nip = self.query_one("#buyer_id_type", Select).value == "nip"
        self.query_one("#buyer_lookup").display = nip
        self.query_one("#buyer_id").set_class(nip, "-nip")

    @on(Checkbox.Changed, "#marks Checkbox")
    def _mark_set(self, event: Checkbox.Changed) -> None:
        if event.value:
            self.query_one("#marks", Collapsible).collapsed = False  # zaznaczone oznaczenie zawsze widać

    @on(Select.Changed)
    @on(Checkbox.Changed)
    def _changed(self, event) -> None:
        if getattr(event, "control", None) and event.control.id == "paid" and event.value:
            paid = self.query_one("#paid_date", Input)
            if not paid.value.strip():
                paid.value = fmt_date(date.today())
        self._update_visibility()

    @on(Input.Changed, "#currency")
    def _currency_changed(self) -> None:
        self._update_visibility()
        self._update_totals()

    def _third_parties_text(self) -> str:
        if not self.draft.third_parties:
            return ""
        parties = ", ".join(f"{t.party.nazwa} ({t.role_label})" for t in self.draft.third_parties)
        return f"[$foreground-muted]Podmioty trzecie (z profilu kontrahenta): {escape(parties)}[/]"

    @on(Button.Pressed, "#buyer_lookup")
    def _buyer_lookup(self) -> None:
        nip = re.sub(r"[\s-]", "", self.query_one("#buyer_id", Input).value).upper().removeprefix("PL")
        if not nip_valid(nip):
            self._invalid("buyer_id", "Wpisz poprawny NIP nabywcy - po nim pobiorę nazwę i adres.")
            return
        self._fetch_buyer(nip)

    @work(exclusive=True, group="buyer-lookup")
    async def _fetch_buyer(self, nip: str) -> None:
        button = self.query_one("#buyer_lookup", Button)
        button.disabled, button.label = True, "Pobieram…"
        try:
            company = await registry.find_company(nip)
        except registry.RegistryError as exc:
            self.notify(str(exc), title="Dane nabywcy", severity="warning", timeout=8)
            return
        finally:
            button.disabled, button.label = False, "Pobierz dane"
        self.query_one("#buyer_id", Input).value = company.nip
        for field, value in (
            ("buyer_name", company.name),
            ("buyer_l1", company.address_l1),
            ("buyer_l2", company.address_l2),
            ("buyer_country", "PL"),
        ):
            self.query_one(f"#{field}", Input).value = value
        text = f"Uzupełniono nazwę i adres: {escape(company.name)}."
        if company.vat_note:
            self.notify(f"{text}\n{company.vat_note}", title="Dane nabywcy", severity="warning", timeout=10)
        else:
            self.notify(text, title="Dane nabywcy", timeout=5)

    @on(Select.Changed, "#buyer_pick")
    def _buyer_picked(self, event: Select.Changed) -> None:
        party = self.buyers.get(str(event.value))
        if party is None:
            return
        id_type = "nip" if party.nip else "vat_ue" if party.vat_ue else "nr_id" if party.nr_id else "brak"
        self.query_one("#buyer_id_type", Select).value = id_type
        self.query_one("#buyer_id", Input).value = party.nip or party.vat_ue or party.nr_id
        for field, value in (
            ("buyer_name", party.nazwa),
            ("buyer_l1", party.adres_l1),
            ("buyer_l2", party.adres_l2),
            ("buyer_country", party.kraj or "PL"),
            ("buyer_email", party.email),
        ):
            self.query_one(f"#{field}", Input).value = value
        self.query_one("#buyer_jst", Checkbox).value = party.jst
        self.query_one("#buyer_gv", Checkbox).value = party.gv
        changed = self._apply_contractor(self.app.profile.contractor(party))
        if changed:
            self.notify("Dla tego nabywcy zmieniono: " + ", ".join(changed) + ".", timeout=6)

    def _apply_contractor(self, contractor: Contractor | None) -> list[str]:
        """Ustawienia nabywcy z profilu (albo domyślne, gdy nabywcy tam nie ma) - poza polami zmienionymi ręcznie.

        Dzięki temu nic nie zostaje po poprzednim nabywcy: termin, waluta, rachunek, powiązania, podmioty trzecie.
        """
        profile = self.app.profile
        changed: list[str] = []
        days = due_days(profile, contractor)
        if self._due_follows_issue and days != self.due_days:
            issue = parse_date(self.query_one("#issue_date", Input).value)
            if issue:
                self.query_one("#due_date", Input).value = fmt_date(issue + timedelta(days=days))
                changed.append(f"termin płatności {days} dni")
        self.due_days = days
        currency_input = self.query_one("#currency", Input)
        currency = (contractor.waluta if contractor and contractor.waluta else "") or profile.invoice.waluta
        if "currency" not in self._touched and currency_input.value.strip().upper() != currency:
            currency_input.value = currency
            changed.append(f"waluta {currency}")
        account_select = self.query_one("#account", Select)
        default = accounts_for(profile, currency)
        if "account" not in self._touched and default and account_select.value != default[0].number:
            with suppress(InvalidSelectValueError):
                account_select.value = default[0].number
                changed.append(f"rachunek …{default[0].number[-4:]}")
        related = contractor.powiazany if contractor else False
        related_box = self.query_one("#related", Checkbox)
        if "related" not in self._touched and related_box.value != related:
            related_box.value = related
            changed.append("powiązania (TP) " + ("zaznaczone" if related else "odznaczone"))
        third = contractor.podmioty3 if contractor else ()
        if third != self.draft.third_parties:
            self.draft = self.draft.with_changes(third_parties=third)
            self.query_one("#third-parties", Static).update(self._third_parties_text())
            changed.append("podmioty trzecie: " + (", ".join(t.party.nazwa for t in third) or "brak"))
        return changed

    @on(Input.Changed, "#issue_date")
    def _issue_changed(self, event: Input.Changed) -> None:
        issue = parse_date(event.value)
        if issue and self._due_follows_issue:
            with self.prevent(Input.Changed):
                self.query_one("#due_date", Input).value = fmt_date(issue + timedelta(days=self.due_days))
        if issue and self._auto_number:
            with self.prevent(Input.Changed):
                self.query_one("#number", Input).value = next_number(self._pattern, self.issued, issue)
        self._update_due_hint()

    @on(Input.Changed, "#number")
    def _number_changed(self) -> None:
        if self.query_one("#number", Input).has_focus:
            self._auto_number = False

    @on(Input.Changed, "#due_date")
    def _due_changed(self) -> None:
        if self.query_one("#due_date", Input).has_focus:
            self._due_follows_issue = False
        self._update_due_hint()

    def _update_due_hint(self) -> None:
        issue = parse_date(self.query_one("#issue_date", Input).value)
        due = parse_date(self.query_one("#due_date", Input).value)
        hint = ""
        if issue and due:
            days = (due - issue).days
            hint = f"{days} dni od wystawienia" if days >= 0 else "przed datą wystawienia!"
        self.query_one("#due_date-hint", Static).update(f"[$foreground-muted]{hint}[/]")

    # ----------------------------------------------------------------- zapis

    def _buyer(self) -> Party | None:
        value = lambda wid: self.query_one(f"#{wid}", Input).value.strip()  # noqa: E731
        id_type = self.query_one("#buyer_id_type", Select).value
        identifier = re.sub(r"\s", "", value("buyer_id"))
        country = value("buyer_country").upper() or "PL"
        if id_type == "nip":
            identifier = identifier.replace("-", "")
            if identifier.upper().startswith("PL"):
                identifier = identifier[2:]
            if not nip_valid(identifier):
                self._invalid("buyer_id", "Niepoprawny NIP nabywcy (zła suma kontrolna).")
                return None
        elif id_type == "vat_ue":
            identifier = identifier.upper()
            if identifier[:2] not in EU_COUNTRIES or not re.fullmatch(r"[A-Z0-9+*]{1,12}", identifier[2:]):
                self._invalid("buyer_id", "Numer VAT UE zaczyna się kodem kraju, np. DE123456789.")
                return None
            if country == "PL":
                country = identifier[:2].replace("EL", "GR")
        elif id_type == "nr_id" and not identifier:
            self._invalid("buyer_id", "Podaj identyfikator podatkowy nabywcy.")
            return None
        return Party(
            nazwa=value("buyer_name"),
            nip=identifier if id_type == "nip" else "",
            vat_ue=identifier if id_type == "vat_ue" else "",
            nr_id=identifier if id_type == "nr_id" else "",
            brak_id=id_type == "brak",
            adres_l1=value("buyer_l1"),
            adres_l2=value("buyer_l2"),
            kraj=country,
            email=value("buyer_email"),
            telefon=self.draft.buyer.telefon,
            nr_klienta=self.draft.buyer.nr_klienta,
            jst=self.query_one("#buyer_jst", Checkbox).value,
            gv=self.query_one("#buyer_gv", Checkbox).value,
        )

    def _invalid(self, field_id: str, message: str) -> None:
        if self._quiet:
            return
        self.notify(message, severity="error", timeout=5)
        self.query_one(f"#{field_id}").focus()

    _quiet = False

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        """Sprawdź fakturę: poprawny projekt trafia do podsumowania (zapisany też jako szkic)."""
        draft = self._collect()
        if draft is None:
            return
        self.save_draft()
        self.dismiss(EditResult(draft, self.draft_id, review=True))

    def _collect(self, *, quiet: bool = False) -> Draft | None:
        """Projekt faktury z pól edytora; None, gdy któreś pole jest niepoprawne (quiet - bez komunikatów)."""
        self._quiet = quiet
        try:
            return self._build()
        finally:
            self._quiet = False

    def _build(self) -> Draft | None:
        inputs = [w for w in self.query(Input) if w.display and w.parent.display]
        for widget in inputs:
            widget.validate(widget.value)
        invalid = [w for w in inputs if not w.is_valid]
        if invalid:
            first = invalid[0]
            self._invalid(first.id or "", f"Popraw pole „{self._labels.get(first.id or '', '')}”.")
            return None
        if not self.lines:
            if not self._quiet:
                self.notify("Dodaj co najmniej jedną pozycję.", severity="error")
                self.action_add_line()
            return None
        buyer = self._buyer()
        if buyer is None:
            return None

        value = lambda wid: self.query_one(f"#{wid}", Input).value.strip()  # noqa: E731
        select = lambda wid: v if (v := self.query_one(f"#{wid}", Select).value) is not Select.NULL else ""  # noqa: E731
        period = None
        sale_date = None
        if select("sale_mode") == "period":
            start, end = parse_date(value("period_from")), parse_date(value("period_to"))
            if not (start and end):
                self._invalid("period_from", "Podaj początek i koniec okresu.")
                return None
            period = (start, end)
        else:
            sale_date = parse_date(value("sale_date"))

        currency = value("currency").upper()
        rate = parse_amount(value("exchange_rate")) if currency != "PLN" else None
        account_number = select("account")
        accounts = tuple(a for a in self.accounts if a.number == account_number)
        paid = self.query_one("#paid", Checkbox).value
        form = str(select("pay_form"))
        payment = Payment(
            form=form,
            other_description=value("pay_other") if form == OTHER_PAYMENT else "",
            due_date=None if paid else parse_date(value("due_date")),
            paid=paid,
            paid_date=parse_date(value("paid_date")) if paid else None,
            accounts=accounts,
        )
        correction = None
        if self.draft.kind == "KOR":
            old = self.draft.correction
            correction = CorrectedInvoice(
                number=value("corr_number"),
                issue_date=parse_date(value("corr_date")) or self.draft.issue_date,
                ksef_number=value("corr_ksef"),
                lines=old.lines if old else (),
                reason=value("corr_reason"),
                effect=str(select("corr_effect")),
            )
        codes = {line.vat_rate for line in self.lines}
        draft = self.draft.with_changes(
            number=value("number"),
            issue_date=parse_date(value("issue_date")),
            sale_date=sale_date,
            period=period,
            place=value("place"),
            currency=currency,
            exchange_rate=rate,
            exchange_rate_info=value("exchange_info") if currency != "PLN" else "",
            buyer=buyer,
            lines=tuple(self.lines),
            payment=payment,
            split_payment=self.query_one("#mpp", Checkbox).value,
            reverse_charge=self.query_one("#reverse", Checkbox).value,
            cash_method=self.query_one("#cash", Checkbox).value,
            self_billing=self.query_one("#self_billing", Checkbox).value,
            related=self.query_one("#related", Checkbox).value,
            exemption_basis=value("exemption") if "zw" in codes else "",
            margin_procedure=str(select("margin")) if "marza" in codes else "",
            notes=value("notes"),
            order_number=value("order_number"),
            contract_number=value("contract_number"),
            footer=value("footer"),
            correction=correction,
            # Kurs przepisany z innej faktury przestaje wymagać sprawdzenia, gdy użytkownik go zmienił.
            review=tuple(
                r for r in self.draft.review if not (r == "exchange_rate" and "exchange_rate" in self._touched)
            ),
        )
        return draft

    def action_save_contractor(self) -> None:
        buyer = self._buyer()
        if buyer is None:
            return
        if not buyer.nazwa or not buyer.adres_l1:
            self.notify("Uzupełnij nazwę i adres nabywcy.", severity="warning")
            return
        profile = self.app.profile
        if profile.contractor(buyer):
            self.notify("Ten kontrahent już jest w profilu.", timeout=3)
            return
        try:
            append_block(profile.path, render_contractor(Contractor(buyer)))
        except OSError as exc:
            self.notify(f"Nie udało się zapisać profilu: {os_error_text(exc)}", severity="error", timeout=8)
            return
        self.app.reload_profile(quiet=True)
        self.buyers[buyer.key] = buyer
        self.notify(f"Zapisano kontrahenta „{buyer.nazwa}” w profilu.", timeout=4)

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        """Zamknij: zmiany zostają w szkicu. Gdy szkicu nie udało się zapisać - pytamy, co zrobić."""
        if self.flush():
            self._leave()
            return

        def chosen(choice: str | None) -> None:
            if choice == "retry":
                self.action_close()
            elif choice == "export":
                self._export_xml()
            elif choice == "drop":
                self._leave(saved=False)

        self.app.push_screen(
            ChoiceScreen(
                "Nie udało się zapisać szkicu",
                [
                    ("Ponów zapis", "spróbuj zapisać szkic jeszcze raz", "retry"),
                    ("Eksportuj XML", "zapisz fakturę jako plik XML w katalogu Pobrane", "export"),
                    ("Porzuć zmiany", "zamknij edycję bez zapisywania", "drop"),
                ],
                intro=f"{escape(self.save_error)}\nWpisane dane nadal są w tym oknie.",
            ),
            chosen,
        )

    def _leave(self, *, saved: bool = True) -> None:
        draft = self._collect(quiet=True)
        self.dismiss(EditResult(draft, self.draft_id if saved else None))

    def _export_xml(self) -> None:
        from ..invoice import build_xml
        from .common import safe_name

        draft = self._collect()
        if draft is None:
            return
        self.app.export_file(
            f"{safe_name(draft.number)}_szkic.xml", build_xml(draft), title="Projekt faktury (niewysłany)"
        )

    @on(Button.Pressed, "#discard")
    def action_discard(self) -> None:
        def confirmed(ok: bool | None) -> None:
            if ok:
                if self.draft_id is not None:
                    self.app.store.delete_draft(self.draft_id)
                self.dismiss(EditResult(None, None, discarded=True))

        self.app.push_screen(
            ConfirmScreen(
                "Porzucić szkic?",
                "Wpisane dane faktury zostaną usunięte. Tego nie można cofnąć.",
                confirm="Porzuć szkic",
            ),
            confirmed,
        )

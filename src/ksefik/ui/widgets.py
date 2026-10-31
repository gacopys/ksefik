"""Wspólne widżety: czytelna prezentacja faktury (używana w podglądzie i w podsumowaniu przed wysyłką)."""

from __future__ import annotations

from decimal import Decimal

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.markup import escape
from textual.widgets import DataTable, Static

from ..codes import VAT_RATES
from ..fa_parser import InvoiceView, PartyView
from ..money import fmt_account, fmt_amount, fmt_date, fmt_qty, parse_date, round2


def fmt_rate(value: Decimal) -> str:
    return format(value.normalize(), "f").replace(".", ",")


def _right(value: str) -> Text:
    return Text(value, justify="right")


def _party_markup(party: PartyView) -> str:
    lines = [f"[b]{escape(party.name or '—')}[/b]"]
    if party.nip:
        lines.append(f"[$foreground-muted]NIP[/] {escape(party.nip)}")
    elif party.other_id:
        lines.append(escape(party.other_id))
    lines += [escape(line) for line in party.address]
    lines += [f"[$foreground-muted]{escape(c)}[/]" for c in party.contact]
    return "\n".join(lines)


def _rows(pairs: list[tuple[str, str]]) -> str:
    width = max((len(label) for label, _ in pairs), default=0)
    return "\n".join(f"[$foreground-muted]{escape(label.ljust(width))}[/]  {value}" for label, value in pairs)


def link(url: str) -> str:
    """Klikalny link (otwiera przeglądarkę) - działa także wtedy, gdy długi adres zawija się na dwie linie."""
    return f"[@click=app.open_link({url!r})]{escape(url)}[/]"


class Card(Static):
    """Ramka z tytułem, w której mieszczą się poszczególne sekcje faktury."""

    def __init__(self, content: str, title: str, *, classes: str = "", **kwargs) -> None:
        super().__init__(content, classes=f"card {classes}".strip(), **kwargs)
        self.border_title = title


class InvoiceDocument(VerticalScroll):
    """Faktura przedstawiona jak dokument: strony, szczegóły, pozycje, podsumowanie i płatność."""

    def __init__(self, view: InvoiceView, *, ksef_number: str = "", verify_url: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.view = view
        self.ksef_number = ksef_number
        self.verify_url = verify_url
        self.can_focus = True

    def compose(self) -> ComposeResult:
        view = self.view
        currency = view.currency

        kind = escape(view.kind or "Faktura")
        if self.ksef_number:
            kind += f"\n[$foreground-muted]Numer KSeF[/] [b]{escape(self.ksef_number)}[/b]"
        with Horizontal(classes="doc-head"):
            yield Static(kind, classes="doc-kind")
            number = f"[$foreground-muted]Numer faktury[/]\n[b]{escape(view.number or '—')}[/b]"
            yield Static(number, classes="doc-number")

        with Horizontal(classes="doc-parties"):
            yield Card(_party_markup(view.seller), "Sprzedawca", classes="party")
            yield Card(_party_markup(view.buyer), "Nabywca", classes="party right")
        for n in range(0, len(view.third_parties), 2):
            with Horizontal(classes="doc-parties"):
                for i, third in enumerate(view.third_parties[n : n + 2]):
                    title = f"Podmiot trzeci: {third.role}" if third.role else "Podmiot trzeci"
                    yield Card(_party_markup(third), title, classes="party right" if i else "party")

        details: list[tuple[str, str]] = [("Data wystawienia", fmt_date(view.issue_date))]
        if view.sale_period:
            start, end = view.sale_period
            details.append(("Okres", f"{fmt_date(start)} – {fmt_date(end)}"))
        elif view.sale_date:
            details.append(("Data wykonania usługi", fmt_date(view.sale_date)))
        if view.issue_place:
            details.append(("Miejsce wystawienia", escape(view.issue_place)))
        details.append(("Waluta", escape(currency)))
        if view.corrected:
            details.append(("Korygowana faktura", escape(", ".join(view.corrected))))
        if view.correction_reason:
            details.append(("Przyczyna korekty", escape(view.correction_reason)))
        for order in view.orders:
            label, _, value = order.partition(": ")
            details.append((label, escape(value)))
        yield Card(_rows(details), "Szczegóły")

        if view.lines:
            lines = DataTable(cursor_type="none", zebra_stripes=True, classes="doc-table")
            lines.border_title = "Pozycje"
            show_gross = any(line.unit_net is None and line.unit_gross is not None for line in view.lines)
            lines.add_column("Lp.")
            lines.add_column("Nazwa towaru lub usługi")
            lines.add_column(_right("Ilość"))
            lines.add_column("J.m.")
            lines.add_column(_right("Cena brutto" if show_gross else "Cena netto"))
            lines.add_column(_right("VAT"))
            lines.add_column(_right("Wartość brutto" if show_gross else "Wartość netto"))
            for line in view.lines:
                price = line.unit_gross if show_gross else line.unit_net
                value = line.gross if show_gross else line.net
                rate = line.rate if not line.rate.isdigit() else f"{line.rate}%"
                name = Text(line.name)
                if line.before:
                    name = Text.assemble(("przed korektą: ", "italic dim"), (line.name, "dim"))
                lines.add_row(
                    line.no,
                    name,
                    _right(fmt_qty(line.quantity) if line.quantity is not None else ""),
                    line.unit,
                    _right(fmt_amount(price)),
                    _right(rate),
                    _right(fmt_amount(value)),
                )
            yield lines

        with Vertical(classes="card doc-summary") as summary:
            summary.border_title = "Podsumowanie"
            if view.rates:
                rates = DataTable(cursor_type="none", classes="doc-table plain")
                rates.add_column("Stawka")
                rates.add_column(_right("Netto"))
                rates.add_column(_right("VAT"))
                rates.add_column(_right("Brutto"))
                for rate in view.rates:
                    rates.add_row(
                        rate.label,
                        _right(fmt_amount(rate.net)),
                        _right(fmt_amount(rate.vat) if rate.vat is not None else "—"),
                        _right(fmt_amount(rate.gross)),
                    )
                yield rates
            total = view.total if view.total is not None else view.net_total + view.vat_total
            # Zapłacona faktura nie jest już „do zapłaty” - pokazujemy jej wartość i datę zapłaty.
            label = "Wartość faktury" if view.paid_on else "Do zapłaty"
            if view.kind_code.startswith("KOR"):
                label = "Do zwrotu" if total < 0 else "Do dopłaty"
            amount = f"{fmt_amount(abs(total))} {escape(currency)}"
            lines = [f"[$foreground-muted]{label}[/]  [b]{amount}[/b]"]
            if view.paid_on and not view.kind_code.startswith("KOR"):
                lines.append(f"[$text-success]Zapłacono {fmt_date(view.paid_on)}[/]")
            if currency != "PLN" and view.exchange_rate:
                rate = f"kurs 1 {escape(currency)} = {fmt_rate(view.exchange_rate)} PLN"
                if view.vat_pln is not None:
                    rate += f" · VAT w PLN {fmt_amount(view.vat_pln)}"
                lines.append(f"[$foreground-muted]{rate}[/]")
            yield Static("\n".join(lines), classes="to-pay")

        payment: list[tuple[str, str]] = []
        if view.payment_form:
            payment.append(("Forma płatności", escape(view.payment_form)))
        for due in view.due_dates:
            payment.append(("Termin płatności", _fmt_due(due)))
        if view.paid_on:
            payment.append(("Zapłacono", fmt_date(view.paid_on)))
        for number, bank in view.bank_accounts:
            suffix = f" [$foreground-muted]({escape(bank)})[/]" if bank else ""
            payment.append(("Rachunek", escape(fmt_account(number)) + suffix))
        if payment:
            yield Card(_rows(payment), "Płatność")

        extra: list[str] = []
        extra += [f"• {escape(a)}" for a in view.annotations]
        extra += [f"• {escape(k)}: {escape(v)}" for k, v in view.notes]
        extra += [f"[$foreground-muted]{escape(f)}[/]" for f in view.footer]
        if extra:
            yield Card("\n".join(extra), "Adnotacje i uwagi")

        if self.verify_url:
            yield Card(
                link(self.verify_url)
                + "\n[$foreground-muted]Kliknij albo naciśnij o, aby otworzyć · l kopiuje link[/]",
                "Link weryfikacyjny KSeF",
            )


def _fmt_due(value: str) -> str:
    parsed = parse_date(value)
    return fmt_date(parsed) if parsed else escape(value)


def amount_preview(net: Decimal | None, vat_rate: str, currency: str = "PLN") -> str:
    """Krótka linijka 'VAT ... · Brutto ...' pokazywana podczas wpisywania kwoty."""
    if net is None or net <= 0:
        return "[$foreground-muted]Wpisz kwotę netto, np. 12 300,00[/]"
    rate = VAT_RATES.get(vat_rate)
    if rate is None or rate.percent is None:
        label = rate.label if rate else vat_rate
        return (
            f"Netto [b]{fmt_amount(net)}[/b]  ·  VAT: {escape(label)}  ·  "
            f"Do zapłaty [b $text-success]{fmt_amount(net)} {currency}[/]"
        )
    vat = round2(net * rate.percent / 100)
    return (
        f"Netto [b]{fmt_amount(net)}[/b]  ·  VAT {rate.label} [b]{fmt_amount(vat)}[/b]  ·  "
        f"Brutto [b $text-success]{fmt_amount(net + vat)} {currency}[/]"
    )

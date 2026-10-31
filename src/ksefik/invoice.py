"""Projekt faktury: domyślne wartości z profilu, kontrola poprawności i budowa XML w schemacie FA(3)."""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import cache
from importlib import resources

from lxml import etree

from .codes import MARGIN_PROCEDURES, SUMMARY_FIELDS, VAT_RATES, VatRate
from .money import fmt_date, nip_valid, round2, round_price
from .profile import BankAccount, CatalogItem, Contractor, InvoiceSettings, Party, Profile, Registers, ThirdParty

NS_FA3 = "http://crd.gov.pl/wzor/2025/06/25/13775/"
SPLIT_PAYMENT_THRESHOLD = Decimal(15000)
OTHER_PAYMENT = "inna"


@dataclass(frozen=True)
class Line:
    name: str
    quantity: Decimal = Decimal(1)
    unit: str = "szt."
    unit_price: Decimal = Decimal(0)
    vat_rate: str = "23"
    gtu: str = ""
    pkwiu: str = ""
    cn: str = ""
    index: str = ""
    annex15: bool = False

    @property
    def net(self) -> Decimal:
        return round2(self.quantity * self.unit_price)

    @property
    def rate(self) -> VatRate:
        return VAT_RATES[self.vat_rate]

    @classmethod
    def from_item(cls, item: CatalogItem, price: Decimal | None = None) -> Line:
        return cls(
            name=item.nazwa,
            unit=item.jednostka,
            vat_rate=item.stawka_vat,
            unit_price=price if price is not None else (item.cena_netto or Decimal(0)),
            gtu=item.gtu,
            pkwiu=item.pkwiu,
            cn=item.cn,
            index=item.indeks,
            annex15=item.zal15,
        )


@dataclass(frozen=True)
class Payment:
    form: str = "6"  # kod formy płatności 1-7, "inna" albo "" (nie podano)
    other_description: str = ""
    due_date: date | None = None
    paid: bool = False
    paid_date: date | None = None
    accounts: tuple[BankAccount, ...] = ()


@dataclass(frozen=True)
class CorrectedInvoice:
    number: str
    issue_date: date
    ksef_number: str = ""  # puste = faktura korygowana wystawiona poza KSeF
    lines: tuple[Line, ...] = ()  # stan przed korektą
    reason: str = ""
    effect: str = ""  # TypKorekty: 1, 2, 3 albo puste


@dataclass(frozen=True)
class RateTotal:
    rate: VatRate
    net: Decimal
    vat: Decimal

    @property
    def gross(self) -> Decimal:
        return self.net + self.vat


@dataclass(frozen=True)
class Draft:
    number: str
    issue_date: date
    seller: Party
    buyer: Party
    lines: tuple[Line, ...]
    sale_date: date | None = None
    period: tuple[date, date] | None = None  # okres zamiast daty wykonania (OkresFa)
    kind: str = "VAT"  # VAT | KOR
    currency: str = "PLN"
    exchange_rate: Decimal | None = None  # kurs do przeliczenia VAT na PLN (faktura w walucie obcej)
    exchange_rate_info: str = ""  # np. "tabela NBP 205/A/NBP/2026 z 22.10.2026"
    place: str = ""
    payment: Payment = field(default_factory=Payment)
    registers: Registers = field(default_factory=Registers)
    third_parties: tuple[ThirdParty, ...] = ()
    cash_method: bool = False
    self_billing: bool = False
    reverse_charge: bool = False
    split_payment: bool = False
    exemption_basis: str = ""
    margin_procedure: str = ""
    related: bool = False
    notes: str = ""
    footer: str = ""
    order_number: str = ""
    contract_number: str = ""
    correction: CorrectedInvoice | None = None
    # Dane przepisane z innej faktury, które użytkownik powinien sprawdzić (np. "exchange_rate") - nie trafiają do XML.
    review: tuple[str, ...] = ()

    # ---------------------------------------------------------------- kwoty

    def totals(self) -> list[RateTotal]:
        """Sumy w podziale na stawki; dla korekty - różnica stanu po i przed korektą."""
        after = rate_totals(self.lines)
        if self.kind != "KOR" or self.correction is None:
            return after
        before = {t.rate.code: t for t in rate_totals(self.correction.lines)}
        result = []
        for total in after:
            prev = before.pop(total.rate.code, RateTotal(total.rate, Decimal(0), Decimal(0)))
            result.append(RateTotal(total.rate, total.net - prev.net, total.vat - prev.vat))
        result += [RateTotal(t.rate, -t.net, -t.vat) for t in before.values()]
        return sorted(result, key=lambda t: list(VAT_RATES).index(t.rate.code))

    @property
    def net(self) -> Decimal:
        return sum((t.net for t in self.totals()), Decimal(0))

    @property
    def vat(self) -> Decimal:
        return sum((t.vat for t in self.totals()), Decimal(0))

    @property
    def gross(self) -> Decimal:
        return self.net + self.vat

    @property
    def gross_pln(self) -> Decimal | None:
        if self.currency == "PLN":
            return self.gross
        return round2(self.gross * self.exchange_rate) if self.exchange_rate else None

    @property
    def foreign(self) -> bool:
        return self.currency != "PLN"

    @property
    def needs_exchange_rate(self) -> bool:
        return self.foreign and any(t.rate.vat_field for t in self.totals())

    def with_changes(self, **changes) -> Draft:
        return replace(self, **changes)


def rate_totals(lines: Iterable[Line]) -> list[RateTotal]:
    """VAT liczony od sumy wartości netto w danej stawce (art. 106e ust. 1 pkt 13-14)."""
    nets: dict[str, Decimal] = {}
    for line in lines:
        nets[line.vat_rate] = nets.get(line.vat_rate, Decimal(0)) + line.net
    result = []
    for code in VAT_RATES:
        if code in nets:
            rate = VAT_RATES[code]
            vat = round2(nets[code] * rate.percent / 100) if rate.percent else Decimal(0)
            result.append(RateTotal(rate, nets[code], vat))
    return result


@dataclass(frozen=True)
class Issue:
    message: str
    blocking: bool = True


# ------------------------------------------------------------- domyślne wartości


def last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def default_period(today: date, cutoff_day: int) -> tuple[int, int]:
    """Do `cutoff_day` włącznie faktura dotyczy poprzedniego miesiąca, później bieżącego."""
    if today.day <= cutoff_day:
        first = today.replace(day=1) - timedelta(days=1)
        return first.year, first.month
    return today.year, today.month


def default_dates(settings: InvoiceSettings, today: date) -> tuple[date, date]:
    """(data wystawienia, data wykonania usługi) według ustawienia `daty` z profilu."""
    if settings.daty == "dzisiaj":
        return today, today
    end = last_day_of_month(*default_period(today, settings.dzien_graniczny))
    if settings.daty == "koniec_miesiaca":
        return end, end
    return today, min(end, today)


_TOKEN = re.compile(r"\{(nr|mm|m|rrrr|rr)(?::(\d+))?\}")


def format_number(pattern: str, counter: int, day: date) -> str:
    def value(match: re.Match[str]) -> str:
        name, width = match.group(1), match.group(2)
        return {
            "nr": str(counter).zfill(int(width or 0)),
            "m": str(day.month),
            "mm": f"{day.month:02d}",
            "rrrr": str(day.year),
            "rr": f"{day.year % 100:02d}",
        }[name]

    return _TOKEN.sub(value, pattern)


def _number_regex(pattern: str, day: date) -> re.Pattern[str]:
    def literal(text: str) -> str:
        # Odstępy wokół i wewnątrz stałych fragmentów są dowolne: "FV/2/10" == "fv / 2 / 10".
        return r"\s*" + "".join(re.escape(ch) + r"\s*" for ch in text if not ch.isspace())

    parts, pos = [], 0
    for match in _TOKEN.finditer(pattern):
        parts.append(literal(pattern[pos : match.start()]))
        parts.append(
            {
                "nr": r"(?P<nr>\d+)",
                "m": rf"0?{day.month}",
                "mm": rf"0?{day.month}",
                "rrrr": str(day.year),
                "rr": f"{day.year % 100:02d}",
            }[match.group(1)]
        )
        pos = match.end()
    parts.append(literal(pattern[pos:]))
    return re.compile("".join(parts), re.IGNORECASE)


def next_number(pattern: str, existing: Iterable[str], day: date) -> str:
    """Kolejny numer według wzoru. Licznik obejmuje miesiąc, rok albo całość - zależnie od elementów wzoru."""
    regex = _number_regex(pattern, day)
    used = [int(m.group("nr")) for n in existing if (m := regex.fullmatch(n or ""))]
    return format_number(pattern, max(used, default=0) + 1, day)


def accounts_for(profile: Profile, currency: str) -> tuple[BankAccount, ...]:
    """Domyślny rachunek: pierwszy w walucie faktury (pole `waluta`), w przeciwnym razie pierwszy z profilu."""
    matching = [a for a in profile.accounts if a.waluta == currency]
    chosen = matching or [a for a in profile.accounts if not a.waluta] or list(profile.accounts)
    return tuple(chosen[:1])


def due_days(profile: Profile, contractor: Contractor | None) -> int:
    if contractor and contractor.termin_platnosci_dni is not None:
        return contractor.termin_platnosci_dni
    return profile.invoice.termin_platnosci_dni


def new_draft(
    profile: Profile,
    today: date,
    existing_numbers: Iterable[str],
    *,
    buyer: Party | None = None,
    item: CatalogItem | None = None,
    net: Decimal | None = None,
) -> Draft:
    """Nowa faktura z ustawień profilu (kontrahent i pozycja domyślne, o ile nie wskazano innych)."""
    contractor = profile.contractor(buyer) if buyer else profile.default_contractor
    buyer = buyer or (contractor.party if contractor else Party())
    item = item or profile.default_item(contractor)
    settings = profile.invoice
    issue, sale = default_dates(settings, today)
    currency = (contractor.waluta if contractor and contractor.waluta else "") or settings.waluta
    price = round2(net) if net is not None else None
    line = Line.from_item(item, price) if item else Line(name="", unit_price=price or Decimal(0))
    form = settings.payment_form
    return Draft(
        number=next_number(settings.numeracja, existing_numbers, issue),
        issue_date=issue,
        sale_date=sale,
        seller=profile.seller,
        buyer=buyer,
        lines=(line,),
        currency=currency,
        place=settings.miejsce_wystawienia,
        payment=Payment(
            form=form,
            due_date=issue + timedelta(days=due_days(profile, contractor)) if form else None,
            accounts=accounts_for(profile, currency) if form in ("6", "") else (),
        ),
        registers=profile.registers,
        third_parties=contractor.podmioty3 if contractor else (),
        cash_method=settings.metoda_kasowa,
        reverse_charge=line.vat_rate in ("oo", "np II"),
        split_payment=settings.mpp,
        exemption_basis=settings.podstawa_zwolnienia,
        margin_procedure=settings.procedura_marzy,
        related=contractor.powiazany if contractor else False,
        footer=settings.stopka,
    )


def as_new(draft: Draft, profile: Profile, today: date, existing_numbers: Iterable[str]) -> Draft:
    """Kopia faktury jako nowa: te same strony i pozycje, nowy numer, daty i termin płatności.

    Rachunek: z profilu w walucie faktury, a gdy takiego nie ma - ten z kopiowanej faktury. Kurs waluty zostaje,
    ale jest oznaczony do sprawdzenia (dotyczył innej daty).
    """
    template = new_draft(profile, today, existing_numbers, buyer=draft.buyer)
    in_currency = tuple(a for a in profile.accounts if a.waluta == draft.currency)[:1]
    accounts = in_currency or draft.payment.accounts or accounts_for(profile, draft.currency)
    payment = replace(template.payment, form=draft.payment.form or template.payment.form)
    if payment.form in ("6", ""):
        payment = replace(payment, accounts=accounts)
    review = ("exchange_rate",) if draft.foreign and draft.exchange_rate else ()
    return replace(
        draft,
        kind="VAT",
        correction=None,
        number=template.number,
        issue_date=template.issue_date,
        sale_date=template.sale_date,
        period=None,
        seller=profile.seller,
        registers=profile.registers,
        payment=payment,
        review=review,
    )


def as_correction(draft: Draft, ksef_number: str, profile: Profile, today: date, existing: Iterable[str]) -> Draft:
    """Faktura korygująca do `draft` (metoda „przed i po”: pozycje do edycji startują od stanu sprzed korekty)."""
    number = next_number(profile.invoice.numeracja_korekt, existing, today)
    return replace(
        draft,
        kind="KOR",
        number=number,
        issue_date=today,
        seller=profile.seller,
        registers=profile.registers,
        payment=replace(draft.payment, due_date=None, paid=False, paid_date=None),
        correction=CorrectedInvoice(
            number=draft.number,
            issue_date=draft.issue_date,
            ksef_number=ksef_number,
            lines=draft.lines,
            reason="",
            effect="",
        ),
    )


# ------------------------------------------------------------ kontrola poprawności


def check_draft(draft: Draft, today: date, reserved_numbers: Iterable[str] = ()) -> list[Issue]:
    """Problemy faktury przed wysyłką. `reserved_numbers` - numery już użyte (wystawione albo w trakcie wysyłki)."""
    issues: list[Issue] = []
    add = issues.append
    number = draft.number.strip()
    if not number:
        add(Issue("Brak numeru faktury."))
    elif number in {n.strip() for n in reserved_numbers}:
        add(Issue(f"Numer {number} jest już użyty na innej fakturze - zmień numer w edycji."))
    if draft.issue_date > today:
        add(
            Issue(
                f"Data wystawienia {fmt_date(draft.issue_date)} jest w przyszłości - KSeF przyjmie fakturę "
                f"dopiero tego dnia. Wyślij ją {fmt_date(draft.issue_date)} albo zmień datę w edycji."
            )
        )
    if draft.period and draft.period[0] > draft.period[1]:
        add(Issue("Początek okresu jest późniejszy niż jego koniec."))

    # Strony
    if not nip_valid(draft.seller.nip):
        add(Issue(f"NIP sprzedawcy {draft.seller.nip!r} jest niepoprawny - popraw profile.toml."))
    buyer = draft.buyer
    if buyer.nip and not nip_valid(buyer.nip):
        add(Issue(f"NIP nabywcy {buyer.nip!r} jest niepoprawny (zła suma kontrolna)."))
    if not (buyer.nip or buyer.vat_ue or buyer.nr_id or buyer.brak_id):
        add(Issue("Podaj NIP nabywcy (albo numer VAT UE, inny identyfikator lub zaznacz „bez identyfikatora”)."))
    if buyer.nip and buyer.nip == draft.seller.nip and not draft.self_billing:
        add(Issue("NIP nabywcy jest taki sam jak sprzedawcy.", blocking=False))
    for label, value in (("nazwa nabywcy", buyer.nazwa), ("adres nabywcy", buyer.adres_l1)):
        if not value.strip():
            add(Issue(f"Pole „{label}” jest puste."))
    roles = {third.rola for third in draft.third_parties}
    if buyer.jst and 8 not in roles:
        add(Issue("Nabywca to jednostka podrzędna JST - dodaj odbiorcę (podmiot 3 z rolą 8) w profilu.", False))
    if buyer.gv and 10 not in roles:
        add(Issue("Nabywca to członek grupy VAT - dodaj członka grupy (podmiot 3 z rolą 10) w profilu.", False))
    if correcting_reason_missing(draft):
        add(Issue("Podaj przyczynę korekty - ułatwi to nabywcy jej rozliczenie.", blocking=False))

    # Pozycje
    correcting = draft.kind == "KOR"
    if not draft.lines:
        add(Issue("Faktura nie ma żadnej pozycji."))
    for n, line in enumerate(draft.lines, 1):
        where = f"Pozycja {n}" if len(draft.lines) > 1 else "Pozycja"
        if not line.name.strip():
            add(Issue(f"{where}: brak nazwy towaru lub usługi."))
        if line.quantity < 0 or (line.quantity == 0 and not correcting):
            add(Issue(f"{where}: ilość musi być większa od zera."))
        if line.unit_price < 0:
            add(Issue(f"{where}: cena nie może być ujemna."))
    if not correcting and draft.lines and draft.net <= 0:
        add(Issue("Wartość faktury musi być większa od zera."))
    if correcting:
        if draft.correction is None or not draft.correction.number.strip():
            add(Issue("Podaj numer faktury korygowanej."))
        elif draft.net == 0 and draft.vat == 0:
            add(Issue("Korekta nie zmienia kwot (to w porządku, jeśli poprawiasz inne dane).", blocking=False))

    codes = {line.vat_rate for line in draft.lines}
    if "zw" in codes and not draft.exemption_basis.strip():
        add(Issue("Pozycje zwolnione z VAT wymagają podstawy zwolnienia (np. art. 113 ust. 1 ustawy o VAT)."))
    if "marza" in codes and draft.margin_procedure not in MARGIN_PROCEDURES:
        add(Issue("Pozycje w procedurze marży wymagają wskazania rodzaju procedury."))
    if codes & {"0 WDT", "np II"} and not buyer.vat_ue:
        add(Issue("Dla WDT i usług dla podatnika z UE podaj numer VAT UE nabywcy.", blocking=False))
    if "oo" in codes and not buyer.nip:
        add(Issue("Odwrotne obciążenie w kraju dotyczy nabywcy z polskim NIP-em.", blocking=False))

    if draft.needs_exchange_rate and not draft.exchange_rate:
        add(Issue(f"Faktura w {draft.currency}: podaj kurs waluty, żeby przeliczyć VAT na złote."))
    elif "exchange_rate" in draft.review and draft.exchange_rate:
        rate = format(draft.exchange_rate.normalize(), "f").replace(".", ",")
        add(
            Issue(
                f"Sprawdź kurs: {rate} PLN za 1 {draft.currency} przepisano z poprzedniej faktury - "
                f"czy jest właściwy na {fmt_date(draft.issue_date)}?",
                blocking=False,
            )
        )
    gross_pln = draft.gross_pln
    if (
        any(line.annex15 for line in draft.lines)
        and gross_pln is not None
        and gross_pln > SPLIT_PAYMENT_THRESHOLD
        and not draft.split_payment
    ):
        add(Issue("Pozycje z załącznika nr 15 powyżej 15 000 zł wymagają mechanizmu podzielonej płatności."))

    payment = draft.payment
    if payment.paid and not payment.paid_date:
        add(Issue("Podaj datę zapłaty."))
    if payment.due_date and payment.due_date < draft.issue_date and not payment.paid:
        add(Issue("Termin płatności jest wcześniejszy niż data wystawienia.", blocking=False))
    if payment.form == "6" and not payment.accounts and not payment.paid:
        add(Issue("Płatność przelewem bez numeru rachunku.", blocking=False))
    if payment.form == OTHER_PAYMENT and not payment.other_description.strip():
        add(Issue("Opisz formę płatności (np. kompensata)."))
    for account in payment.accounts:
        if not 10 <= len(account.number) <= 34:
            add(Issue(f"Numer rachunku {account.numer!r} ma niepoprawną długość."))
    return issues


def correcting_reason_missing(draft: Draft) -> bool:
    return draft.kind == "KOR" and draft.correction is not None and not draft.correction.reason.strip()


# --------------------------------------------------------------------- XML FA(3)


def _num(value: Decimal) -> str:
    return f"{round2(value):.2f}"


def _qty(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


def summary_fields(draft: Draft) -> dict[str, Decimal]:
    """Pola P_13_x / P_14_x / P_14_xW (sumy według stawek, w kolejności z XSD)."""
    sums: dict[str, Decimal] = {}
    for total in draft.totals():
        rate = total.rate
        sums[rate.net_field] = sums.get(rate.net_field, Decimal(0)) + total.net
        if rate.vat_field:
            sums[rate.vat_field] = sums.get(rate.vat_field, Decimal(0)) + total.vat
            if draft.foreign and draft.exchange_rate and rate.vat_pln_field:
                vat_pln = round2(total.vat * draft.exchange_rate)
                sums[rate.vat_pln_field] = sums.get(rate.vat_pln_field, Decimal(0)) + vat_pln
    return {name: sums[name] for name in SUMMARY_FIELDS if name in sums}


def build_xml(draft: Draft, created: datetime | None = None) -> bytes:
    created = (created or datetime.now(UTC)).astimezone(UTC)

    def el(parent: etree._Element, name: str, text: str | None = None, **attrs: str) -> etree._Element:
        node = etree.SubElement(parent, f"{{{NS_FA3}}}{name}", **attrs)
        if text is not None:
            node.text = text
        return node

    def address(parent: etree._Element, party: Party) -> None:
        if not party.adres_l1.strip():
            return
        adr = el(parent, "Adres")
        el(adr, "KodKraju", party.kraj or "PL")
        el(adr, "AdresL1", party.adres_l1.strip())
        if party.adres_l2.strip():
            el(adr, "AdresL2", party.adres_l2.strip())

    def contact(parent: etree._Element, party: Party) -> None:
        if party.email.strip() or party.telefon.strip():
            node = el(parent, "DaneKontaktowe")
            if party.email.strip():
                el(node, "Email", party.email.strip())
            if party.telefon.strip():
                el(node, "Telefon", party.telefon.strip())

    def identification(parent: etree._Element, party: Party) -> None:
        ids = el(parent, "DaneIdentyfikacyjne")
        if party.nip:
            el(ids, "NIP", party.nip)
        elif party.vat_ue:
            el(ids, "KodUE", party.vat_ue[:2])
            el(ids, "NrVatUE", party.vat_ue[2:])
        elif party.nr_id:
            el(ids, "KodKraju", party.kraj or "PL")
            el(ids, "NrID", party.nr_id.strip())
        else:
            el(ids, "BrakID", "1")
        if party.nazwa.strip():
            el(ids, "Nazwa", party.nazwa.strip())

    root = etree.Element(f"{{{NS_FA3}}}Faktura", nsmap={None: NS_FA3})

    header = el(root, "Naglowek")
    el(header, "KodFormularza", "FA", kodSystemowy="FA (3)", wersjaSchemy="1-0E")
    el(header, "WariantFormularza", "3")
    el(header, "DataWytworzeniaFa", created.strftime("%Y-%m-%dT%H:%M:%SZ"))
    el(header, "SystemInfo", "ksefik")

    p1 = el(root, "Podmiot1")
    ids = el(p1, "DaneIdentyfikacyjne")
    el(ids, "NIP", draft.seller.nip)
    el(ids, "Nazwa", draft.seller.nazwa.strip())
    address(p1, draft.seller)
    contact(p1, draft.seller)

    p2 = el(root, "Podmiot2")
    identification(p2, draft.buyer)
    address(p2, draft.buyer)
    contact(p2, draft.buyer)
    if draft.buyer.nr_klienta.strip():
        el(p2, "NrKlienta", draft.buyer.nr_klienta.strip())
    el(p2, "JST", "1" if draft.buyer.jst else "2")
    el(p2, "GV", "1" if draft.buyer.gv else "2")

    for third in draft.third_parties:
        p3 = el(root, "Podmiot3")
        identification(p3, third.party)
        address(p3, third.party)
        contact(p3, third.party)
        if third.rola:
            el(p3, "Rola", str(third.rola))
        else:
            el(p3, "RolaInna", "1")
            el(p3, "OpisRoli", third.opis_roli.strip() or "Inny podmiot")
        if third.udzial is not None:
            el(p3, "Udzial", f"{third.udzial:.2f}")

    fa = el(root, "Fa")
    el(fa, "KodWaluty", draft.currency)
    el(fa, "P_1", draft.issue_date.isoformat())
    if draft.place.strip():
        el(fa, "P_1M", draft.place.strip())
    el(fa, "P_2", draft.number.strip())
    if draft.period:
        period = el(fa, "OkresFa")
        el(period, "P_6_Od", draft.period[0].isoformat())
        el(period, "P_6_Do", draft.period[1].isoformat())
    elif draft.sale_date:
        el(fa, "P_6", draft.sale_date.isoformat())
    for name, value in summary_fields(draft).items():
        el(fa, name, _num(value))
    el(fa, "P_15", _num(draft.gross))

    codes = {line.vat_rate for line in draft.lines}
    notes = el(fa, "Adnotacje")
    el(notes, "P_16", "1" if draft.cash_method else "2")
    el(notes, "P_17", "1" if draft.self_billing else "2")
    el(notes, "P_18", "1" if draft.reverse_charge or "oo" in codes else "2")
    el(notes, "P_18A", "1" if draft.split_payment else "2")
    exemption = el(notes, "Zwolnienie")
    if "zw" in codes:
        el(exemption, "P_19", "1")
        basis = draft.exemption_basis.strip()
        el(exemption, "P_19B" if "dyrektyw" in basis.lower() else "P_19A", basis)
    else:
        el(exemption, "P_19N", "1")
    el(el(notes, "NoweSrodkiTransportu"), "P_22N", "1")
    el(notes, "P_23", "2")
    margin = el(notes, "PMarzy")
    if "marza" in codes:
        el(margin, "P_PMarzy", "1")
        el(margin, MARGIN_PROCEDURES[draft.margin_procedure][0], "1")
    else:
        el(margin, "P_PMarzyN", "1")
    el(fa, "RodzajFaktury", draft.kind)

    if draft.kind == "KOR" and draft.correction:
        corr = draft.correction
        if corr.reason.strip():
            el(fa, "PrzyczynaKorekty", corr.reason.strip())
        if corr.effect:
            el(fa, "TypKorekty", corr.effect)
        data = el(fa, "DaneFaKorygowanej")
        el(data, "DataWystFaKorygowanej", corr.issue_date.isoformat())
        el(data, "NrFaKorygowanej", corr.number.strip())
        if corr.ksef_number:
            el(data, "NrKSeF", "1")
            el(data, "NrKSeFFaKorygowanej", corr.ksef_number)
        else:
            el(data, "NrKSeFN", "1")

    if draft.related:
        el(fa, "TP", "1")
    descriptions = []
    if draft.notes.strip():
        descriptions.append(("Uwagi", draft.notes.strip()))
    if draft.foreign and draft.exchange_rate:
        info = f"1 {draft.currency} = {_qty(draft.exchange_rate)} PLN"
        if draft.exchange_rate_info.strip():
            info += f", {draft.exchange_rate_info.strip()}"
        descriptions.append(("Kurs waluty", info))
    for key, value in descriptions:
        desc = el(fa, "DodatkowyOpis")
        el(desc, "Klucz", key)
        el(desc, "Wartosc", value[:256])

    rows: list[tuple[Line, bool]] = []
    if draft.kind == "KOR" and draft.correction:
        before = list(draft.correction.lines)
        for i in range(max(len(before), len(draft.lines))):
            if i < len(before):
                rows.append((before[i], True))
            after = draft.lines[i] if i < len(draft.lines) else replace(before[i], quantity=Decimal(0))
            rows.append((after, False))
    else:
        rows = [(line, False) for line in draft.lines]
    for n, (line, before_state) in enumerate(rows, 1):
        row = el(fa, "FaWiersz")
        el(row, "NrWierszaFa", str(n))
        el(row, "P_7", line.name.strip())
        if line.index.strip():
            el(row, "Indeks", line.index.strip())
        if line.pkwiu.strip():
            el(row, "PKWiU", line.pkwiu.strip())
        if line.cn.strip():
            el(row, "CN", line.cn.strip())
        el(row, "P_8A", line.unit.strip() or "szt.")
        el(row, "P_8B", _qty(line.quantity))
        el(row, "P_9A", _qty(round_price(line.unit_price)))
        el(row, "P_11", _num(line.net))
        if line.vat_rate != "marza":
            el(row, "P_12", line.vat_rate)
        if line.annex15:
            el(row, "P_12_Zal_15", "1")
        if line.gtu:
            el(row, "GTU", line.gtu)
        if draft.foreign and draft.exchange_rate and line.rate.vat_field:
            el(row, "KursWaluty", _qty(draft.exchange_rate))
        if before_state:
            el(row, "StanPrzed", "1")

    payment = draft.payment
    if payment.paid or payment.due_date or payment.form or payment.accounts:
        pay = el(fa, "Platnosc")
        if payment.paid:
            el(pay, "Zaplacono", "1")
            el(pay, "DataZaplaty", (payment.paid_date or draft.issue_date).isoformat())
        elif payment.due_date:
            el(el(pay, "TerminPlatnosci"), "Termin", payment.due_date.isoformat())
        if payment.form == OTHER_PAYMENT:
            el(pay, "PlatnoscInna", "1")
            el(pay, "OpisPlatnosci", payment.other_description.strip() or "Inna")
        elif payment.form:
            el(pay, "FormaPlatnosci", payment.form)
        for account in payment.accounts:
            acc = el(pay, "RachunekBankowy")
            el(acc, "NrRB", account.number)
            if account.swift.strip():
                el(acc, "SWIFT", account.swift.strip().upper())
            if account.bank.strip():
                el(acc, "NazwaBanku", account.bank.strip())
            if account.opis.strip():
                el(acc, "OpisRachunku", account.opis.strip())

    if draft.contract_number.strip() or draft.order_number.strip():
        terms = el(fa, "WarunkiTransakcji")
        if draft.contract_number.strip():
            el(el(terms, "Umowy"), "NrUmowy", draft.contract_number.strip())
        if draft.order_number.strip():
            el(el(terms, "Zamowienia"), "NrZamowienia", draft.order_number.strip())

    if draft.footer.strip() or draft.registers:
        footer = el(root, "Stopka")
        if draft.footer.strip():
            el(el(footer, "Informacje"), "StopkaFaktury", draft.footer.strip())
        if draft.registers:
            reg = el(footer, "Rejestry")
            registers = draft.registers
            for name, value in (("KRS", registers.krs), ("REGON", registers.regon), ("BDO", registers.bdo)):
                if value.strip():
                    el(reg, name, value.strip())

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


@cache
def _schema() -> etree.XMLSchema:
    path = resources.files("ksefik") / "schemas" / "schemat_FA3_v1-0E.xsd"
    with resources.as_file(path) as xsd:
        return etree.XMLSchema(etree.parse(str(xsd)))


def validate_xml(xml: bytes) -> list[str]:
    """Walidacja względem oficjalnego XSD FA(3). Zwraca listę błędów (pusta = OK)."""
    try:
        document = etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:
        return [f"niepoprawny XML: {exc}"]
    schema = _schema()
    if schema.validate(document):
        return []
    return [f"wiersz {e.line}: {e.message}" for e in schema.error_log]


def is_fa3(xml: bytes) -> bool:
    try:
        return etree.QName(etree.fromstring(xml)).namespace == NS_FA3
    except etree.XMLSyntaxError:
        return False

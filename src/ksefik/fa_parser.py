"""Odczyt faktur FA(2)/FA(3): model do wyświetlania oraz projekt faktury do ponownego wystawienia lub korekty."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from lxml import etree

from .codes import INVOICE_KINDS, MARGIN_PROCEDURES, PAYMENT_FORMS, SUMMARY_LABELS, THIRD_PARTY_ROLES, VAT_RATES
from .invoice import Draft, Line, Payment
from .money import round_price
from .profile import BankAccount, Party, ThirdParty


@dataclass
class PartyView:
    name: str = ""
    nip: str = ""
    other_id: str = ""
    address: list[str] = field(default_factory=list)
    contact: list[str] = field(default_factory=list)
    role: str = ""


@dataclass
class LineView:
    no: str
    name: str
    unit: str
    quantity: Decimal | None
    unit_net: Decimal | None
    unit_gross: Decimal | None
    net: Decimal | None
    gross: Decimal | None
    rate: str
    before: bool = False  # stan przed korektą


@dataclass
class RateView:
    label: str
    net: Decimal
    vat: Decimal | None

    @property
    def gross(self) -> Decimal:
        return self.net + (self.vat or Decimal(0))


@dataclass
class InvoiceView:
    number: str = ""
    kind: str = ""
    kind_code: str = ""
    currency: str = "PLN"
    issue_date: date | None = None
    sale_date: date | None = None
    sale_period: tuple[date | None, date | None] | None = None
    issue_place: str = ""
    seller: PartyView = field(default_factory=PartyView)
    buyer: PartyView = field(default_factory=PartyView)
    third_parties: list[PartyView] = field(default_factory=list)
    lines: list[LineView] = field(default_factory=list)
    rates: list[RateView] = field(default_factory=list)
    total: Decimal | None = None
    annotations: list[str] = field(default_factory=list)
    payment_form: str = ""
    due_dates: list[str] = field(default_factory=list)
    paid_on: date | None = None
    bank_accounts: list[tuple[str, str]] = field(default_factory=list)  # (numer, nazwa banku)
    corrected: list[str] = field(default_factory=list)
    correction_reason: str = ""
    notes: list[tuple[str, str]] = field(default_factory=list)
    orders: list[str] = field(default_factory=list)
    footer: list[str] = field(default_factory=list)
    exchange_rate: Decimal | None = None  # kurs waluty z pozycji (faktura w walucie obcej)
    vat_pln: Decimal | None = None  # VAT przeliczony na złote (pola P_14_xW)

    @property
    def net_total(self) -> Decimal:
        return sum((r.net for r in self.rates), Decimal(0))

    @property
    def vat_total(self) -> Decimal:
        return sum((r.vat or Decimal(0) for r in self.rates), Decimal(0))


def _local(tag: object) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _child(node: etree._Element | None, *path: str) -> etree._Element | None:
    for name in path:
        if node is None:
            return None
        node = next((c for c in node if _local(c.tag) == name), None)
    return node


def _children(node: etree._Element | None, name: str) -> list[etree._Element]:
    if node is None:
        return []
    return [c for c in node if _local(c.tag) == name]


def _text(node: etree._Element | None, *path: str) -> str:
    found = _child(node, *path) if path else node
    return (found.text or "").strip() if found is not None else ""


def _dec(text: str) -> Decimal | None:
    try:
        return Decimal(text) if text else None
    except InvalidOperation:
        return None


def _date(text: str) -> date | None:
    try:
        return date.fromisoformat(text[:10]) if text else None
    except ValueError:
        return None


def _party(node: etree._Element | None) -> PartyView:
    party = PartyView()
    if node is None:
        return party
    ids = _child(node, "DaneIdentyfikacyjne")
    party.name = _text(ids, "Nazwa")
    party.nip = _text(ids, "NIP")
    if not party.nip:
        if vat_ue := _text(ids, "NrVatUE"):
            party.other_id = f"VAT UE: {_text(ids, 'KodUE')}{vat_ue}"
        elif other := _text(ids, "NrID"):
            party.other_id = f"ID: {_text(ids, 'KodKraju')} {other}".strip()
        elif internal := _text(ids, "IDWew"):
            party.other_id = f"Id. wewnętrzny: {internal}"
        elif _text(ids, "BrakID") == "1":
            party.other_id = "bez identyfikatora podatkowego"
    adr = _child(node, "Adres")
    if adr is not None:
        party.address = [t for t in (_text(adr, "AdresL1"), _text(adr, "AdresL2")) if t]
        country = _text(adr, "KodKraju")
        if country and country != "PL":
            party.address.append(country)
    for contact in _children(node, "DaneKontaktowe"):
        party.contact += [t for t in (_text(contact, "Email"), _text(contact, "Telefon")) if t]
    if role := _text(node, "Rola"):
        party.role = THIRD_PARTY_ROLES.get(int(role), role) if role.isdigit() else role
    elif _text(node, "RolaInna") == "1":
        party.role = _text(node, "OpisRoli") or "Inna rola"
    return party


def parse_invoice(xml: bytes) -> InvoiceView:
    root = etree.fromstring(xml)
    inv = InvoiceView()
    inv.seller = _party(_child(root, "Podmiot1"))
    inv.buyer = _party(_child(root, "Podmiot2"))
    inv.third_parties = [_party(node) for node in _children(root, "Podmiot3")]

    fa = _child(root, "Fa")
    if fa is None:
        return inv
    inv.currency = _text(fa, "KodWaluty") or "PLN"
    inv.issue_date = _date(_text(fa, "P_1"))
    inv.issue_place = _text(fa, "P_1M")
    inv.number = _text(fa, "P_2")
    inv.sale_date = _date(_text(fa, "P_6"))
    if (period := _child(fa, "OkresFa")) is not None:
        inv.sale_period = (_date(_text(period, "P_6_Od")), _date(_text(period, "P_6_Do")))
    inv.kind_code = _text(fa, "RodzajFaktury")
    inv.kind = INVOICE_KINDS.get(inv.kind_code, inv.kind_code)

    for net_field, vat_field, label in SUMMARY_LABELS:
        net = _dec(_text(fa, net_field))
        if net is not None:
            vat = _dec(_text(fa, vat_field)) if vat_field else None
            inv.rates.append(RateView(label=label, net=net, vat=vat))
    inv.total = _dec(_text(fa, "P_15"))
    vat_pln = [v for name in ("P_14_1W", "P_14_2W", "P_14_3W", "P_14_4W") if (v := _dec(_text(fa, name))) is not None]
    inv.vat_pln = sum(vat_pln, Decimal(0)) if vat_pln else None

    notes = _child(fa, "Adnotacje")
    if _text(notes, "P_16") == "1":
        inv.annotations.append("Metoda kasowa")
    if _text(notes, "P_17") == "1":
        inv.annotations.append("Samofakturowanie")
    if _text(notes, "P_18") == "1":
        inv.annotations.append("Odwrotne obciążenie")
    if _text(notes, "P_18A") == "1":
        inv.annotations.append("Mechanizm podzielonej płatności")
    exemption = _child(notes, "Zwolnienie")
    if _text(exemption, "P_19") == "1":
        basis = _text(exemption, "P_19A") or _text(exemption, "P_19B") or _text(exemption, "P_19C")
        inv.annotations.append(f"Zwolnienie z VAT: {basis}" if basis else "Zwolnienie z VAT")
    if _text(notes, "P_23") == "1":
        inv.annotations.append("Procedura uproszczona (transakcja trójstronna)")
    margin = _child(notes, "PMarzy")
    if _text(margin, "P_PMarzy") == "1":
        labels = [label for fld, label in MARGIN_PROCEDURES.values() if _text(margin, fld) == "1"]
        inv.annotations.append(labels[0] if labels else "Procedura marży")
    if _text(fa, "TP") == "1":
        inv.annotations.append("Istnieją powiązania między nabywcą a sprzedawcą (TP)")

    if reason := _text(fa, "PrzyczynaKorekty"):
        inv.correction_reason = reason
    for corrected in _children(fa, "DaneFaKorygowanej"):
        number = _text(corrected, "NrFaKorygowanej")
        ksef = _text(corrected, "NrKSeFFaKorygowanej")
        issued = _date(_text(corrected, "DataWystFaKorygowanej"))
        label = f"{number} z {issued:%d.%m.%Y}" if issued else number
        inv.corrected.append(f"{label} ({ksef})" if ksef else label)

    for desc in _children(fa, "DodatkowyOpis"):
        inv.notes.append((_text(desc, "Klucz"), _text(desc, "Wartosc")))

    rates = [_dec(_text(row, "KursWaluty")) for row in _children(fa, "FaWiersz")]
    inv.exchange_rate = next((r for r in rates if r), None) or _dec(_text(fa, "KursWalutyZ"))
    for row in _children(fa, "FaWiersz"):
        inv.lines.append(
            LineView(
                no=_text(row, "NrWierszaFa"),
                name=_text(row, "P_7"),
                unit=_text(row, "P_8A"),
                quantity=_dec(_text(row, "P_8B")),
                unit_net=_dec(_text(row, "P_9A")),
                unit_gross=_dec(_text(row, "P_9B")),
                net=_dec(_text(row, "P_11")),
                gross=_dec(_text(row, "P_11A")),
                rate=_text(row, "P_12"),
                before=_text(row, "StanPrzed") == "1",
            )
        )

    payment = _child(fa, "Platnosc")
    if payment is not None:
        if _text(payment, "Zaplacono") == "1":
            inv.paid_on = _date(_text(payment, "DataZaplaty"))
        inv.payment_form = PAYMENT_FORMS.get(_text(payment, "FormaPlatnosci"), "")
        if not inv.payment_form and _text(payment, "PlatnoscInna") == "1":
            inv.payment_form = _text(payment, "OpisPlatnosci") or "Inna"
        for term in _children(payment, "TerminPlatnosci"):
            if when := _text(term, "Termin"):
                inv.due_dates.append(when)
            elif (opis := _child(term, "TerminOpis")) is not None:
                inv.due_dates.append(
                    f"{_text(opis, 'Ilosc')} {_text(opis, 'Jednostka')} od {_text(opis, 'ZdarzeniePoczatkowe')}"
                )
        for account in _children(payment, "RachunekBankowy"):
            if number := _text(account, "NrRB"):
                inv.bank_accounts.append((number, _text(account, "NazwaBanku")))

    terms = _child(fa, "WarunkiTransakcji")
    for contract in _children(terms, "Umowy"):
        if number := _text(contract, "NrUmowy"):
            inv.orders.append(f"Umowa: {number}")
    for order in _children(terms, "Zamowienia"):
        if number := _text(order, "NrZamowienia"):
            inv.orders.append(f"Zamówienie: {number}")

    footer = _child(root, "Stopka")
    for info in _children(footer, "Informacje"):
        if text := _text(info, "StopkaFaktury"):
            inv.footer.append(text)
    for reg in _children(footer, "Rejestry"):
        parts = [f"{k}: {v}" for k in ("KRS", "REGON", "BDO") if (v := _text(reg, k))]
        if parts:
            inv.footer.append(", ".join(parts))
    return inv


def searchable_text(xml: bytes) -> str:
    """Wszystkie wartości tekstowe faktury - do wyszukiwania pełnotekstowego."""
    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError:
        return ""
    return " ".join(t.strip() for t in root.itertext() if t.strip())


# --------------------------------------------------------- XML -> projekt faktury


def _party_data(node: etree._Element | None) -> dict[str, object]:
    """Dane strony faktury w formacie pól klasy profile.Party."""
    if node is None:
        return {}
    ids = _child(node, "DaneIdentyfikacyjne")
    adr = _child(node, "Adres")
    contact = _child(node, "DaneKontaktowe")
    data: dict[str, object] = {
        "nazwa": _text(ids, "Nazwa"),
        "nip": _text(ids, "NIP"),
        "adres_l1": _text(adr, "AdresL1"),
        "adres_l2": _text(adr, "AdresL2"),
        "kraj": _text(adr, "KodKraju") or _text(ids, "KodKraju") or "PL",
        "email": _text(contact, "Email"),
        "telefon": _text(contact, "Telefon"),
        "nr_klienta": _text(node, "NrKlienta"),
        "jst": _text(node, "JST") == "1",
        "gv": _text(node, "GV") == "1",
    }
    if not data["nip"]:
        if vat_ue := _text(ids, "NrVatUE"):
            data["vat_ue"] = _text(ids, "KodUE") + vat_ue
        elif other := _text(ids, "NrID"):
            data["nr_id"] = other
        else:
            data["brak_id"] = _text(ids, "BrakID") == "1"
    return data


def parse_draft(xml: bytes) -> Draft:
    """Projekt faktury odtworzony z XML (do wystawienia podobnej faktury albo korekty)."""
    root = etree.fromstring(xml)
    fa = _child(root, "Fa")
    if fa is None:
        raise ValueError("To nie jest faktura FA - brak elementu Fa.")

    lines = []
    for row in _children(fa, "FaWiersz"):
        if _text(row, "StanPrzed") == "1":
            continue
        quantity = _dec(_text(row, "P_8B"))
        if quantity is None:
            quantity = Decimal(1)
        rate = _text(row, "P_12") or "marza"
        price = _dec(_text(row, "P_9A"))
        if price is None:
            net = _dec(_text(row, "P_11"))
            gross_price = _dec(_text(row, "P_9B"))
            if net is not None:
                price = round_price(net / quantity) if quantity else net
            elif gross_price is not None:
                percent = VAT_RATES[rate].percent if rate in VAT_RATES else None
                price = round_price(gross_price / (1 + percent / 100)) if percent else gross_price
        lines.append(
            Line(
                name=_text(row, "P_7"),
                quantity=quantity,
                unit=_text(row, "P_8A") or "szt.",
                unit_price=price or Decimal(0),
                vat_rate=rate if rate in VAT_RATES else "23",
                gtu=_text(row, "GTU"),
                pkwiu=_text(row, "PKWiU"),
                cn=_text(row, "CN"),
                index=_text(row, "Indeks"),
                annex15=_text(row, "P_12_Zal_15") == "1",
            )
        )

    payment_node = _child(fa, "Platnosc")
    form = _text(payment_node, "FormaPlatnosci")
    accounts = tuple(
        BankAccount(_text(a, "NrRB"), _text(a, "NazwaBanku"), _text(a, "SWIFT"), _text(a, "OpisRachunku"))
        for a in _children(payment_node, "RachunekBankowy")
        if _text(a, "NrRB")
    )
    other = _text(payment_node, "PlatnoscInna") == "1"
    payment = Payment(
        form="inna" if other else form,
        other_description=_text(payment_node, "OpisPlatnosci"),
        due_date=_date(_text(payment_node, "TerminPlatnosci", "Termin")),
        paid=_text(payment_node, "Zaplacono") == "1",
        paid_date=_date(_text(payment_node, "DataZaplaty")),
        accounts=accounts,
    )

    third = []
    for node in _children(root, "Podmiot3"):
        role = _text(node, "Rola")
        share = _dec(_text(node, "Udzial"))
        third.append(
            ThirdParty(
                Party(**_party_data(node)),
                rola=int(role) if role.isdigit() else None,
                opis_roli=_text(node, "OpisRoli"),
                udzial=share,
            )
        )

    notes_node = _child(fa, "Adnotacje")
    exemption = _child(notes_node, "Zwolnienie")
    margin = _child(notes_node, "PMarzy")
    descriptions = {_text(d, "Klucz"): _text(d, "Wartosc") for d in _children(fa, "DodatkowyOpis")}
    rate_text = next((_text(r, "KursWaluty") for r in _children(fa, "FaWiersz") if _text(r, "KursWaluty")), "")
    period = _child(fa, "OkresFa")
    terms = _child(fa, "WarunkiTransakcji")
    footer = _child(root, "Stopka")
    return Draft(
        number=_text(fa, "P_2"),
        issue_date=_date(_text(fa, "P_1")) or date.today(),
        sale_date=_date(_text(fa, "P_6")),
        period=(_date(_text(period, "P_6_Od")), _date(_text(period, "P_6_Do"))) if period is not None else None,
        seller=Party(**_party_data(_child(root, "Podmiot1"))),
        buyer=Party(**_party_data(_child(root, "Podmiot2"))),
        lines=tuple(lines),
        currency=_text(fa, "KodWaluty") or "PLN",
        exchange_rate=_dec(rate_text),
        place=_text(fa, "P_1M"),
        payment=payment,
        third_parties=tuple(third),
        cash_method=_text(notes_node, "P_16") == "1",
        self_billing=_text(notes_node, "P_17") == "1",
        reverse_charge=_text(notes_node, "P_18") == "1",
        split_payment=_text(notes_node, "P_18A") == "1",
        exemption_basis=_text(exemption, "P_19A") or _text(exemption, "P_19B") or _text(exemption, "P_19C"),
        margin_procedure=next((k for k, (f, _) in MARGIN_PROCEDURES.items() if _text(margin, f) == "1"), ""),
        related=_text(fa, "TP") == "1",
        notes=descriptions.get("Uwagi", ""),
        footer=_text(_child(footer, "Informacje"), "StopkaFaktury"),
        order_number=_text(_child(terms, "Zamowienia"), "NrZamowienia"),
        contract_number=_text(_child(terms, "Umowy"), "NrUmowy"),
    )


# Elementy FA(3), których projekt faktury (Draft) nie przenosi - przy „wystaw podobną” i korekcie by zniknęły.
# Dane sprzedawcy i stopka zawsze pochodzą z bieżącego profilu, więc nie są tu wymieniane.
UNSUPPORTED_ELEMENTS = {
    "Zalacznik": "załącznik do faktury",
    "PodmiotUpowazniony": "podmiot upoważniony",
    "AdresKoresp": "adres korespondencyjny",
    "IDNabywcy": "identyfikator nabywcy (IDNabywcy)",
    "NrEORI": "numer EORI",
    "WZ": "numery dokumentów WZ",
    "FP": "oznaczenie faktury do paragonu (FP)",
    "KursWalutyZ": "kurs waluty zaliczki",
    "ZaliczkaCzesciowa": "zaliczki częściowe",
    "FakturaZaliczkowa": "powiązane faktury zaliczkowe",
    "Zamowienie": "zamówienie (faktura zaliczkowa)",
    "Rozliczenie": "rozliczenie (obciążenia i odliczenia)",
    "OkresFaKorygowanej": "okres faktury korygowanej",
    "NrFaKorygowany": "poprawiony numer faktury korygowanej",
    "Podmiot1K": "dane sprzedawcy przed korektą",
    "Podmiot2K": "dane nabywcy przed korektą",
    "ZwrotAkcyzy": "zwrot akcyzy",
    "TerminOpis": "opisowy termin płatności",
    "Skonto": "skonto",
    "ZaplataCzesciowa": "zapłaty częściowe",
    "RachunekBankowyFaktora": "rachunek faktora",
    "LinkDoPlatnosci": "link do płatności",
    "P_6A": "daty dostawy w pozycjach",
    "P_10": "rabaty w pozycjach",
    "GTIN": "kody GTIN",
    "PKOB": "kody PKOB",
    "KwotaAkcyzy": "akcyza w pozycjach",
    "Procedura": "procedury w pozycjach",
    "P_12_XII": "stawka OSS w pozycjach",
    "DataUmowy": "daty umów",
    "DataZamowienia": "daty zamówień",
    "NrPartiiTowaru": "numery partii towaru",
    "WarunkiDostawy": "warunki dostawy",
    "KursUmowny": "kurs umowny",
    "Transport": "dane transportu",
    "PodmiotPosredniczacy": "podmiot pośredniczący",
}
DESCRIPTION_KEYS = {"Uwagi", "Kurs waluty"}


def draft_gaps(xml: bytes) -> list[str]:
    """Dane faktury, których projekt (parse_draft) nie przenosi - do pokazania przed „wystaw podobną” lub korektą."""
    root = etree.fromstring(xml)
    gaps: list[str] = []

    def add(label: str) -> None:
        if label not in gaps:
            gaps.append(label)

    for node in root.iter():
        name = _local(node.tag)
        if name in UNSUPPORTED_ELEMENTS and _local(node.getparent().tag) != "Podmiot1":
            add(UNSUPPORTED_ELEMENTS[name])
    fa = _child(root, "Fa")
    notes = _child(fa, "Adnotacje")
    if _text(notes, "NoweSrodkiTransportu", "P_22") == "1":
        add("dostawa nowych środków transportu")
    if _text(notes, "P_23") == "1":
        add("procedura uproszczona (transakcja trójstronna)")
    for key in [_text(d, "Klucz") for d in _children(fa, "DodatkowyOpis")]:
        if key not in DESCRIPTION_KEYS:
            add(f"dodatkowy opis „{key}”")
    payment = _child(fa, "Platnosc")
    if len(_children(payment, "TerminPlatnosci")) > 1:
        add("kolejne terminy płatności")
    terms = _child(fa, "WarunkiTransakcji")
    if len(_children(terms, "Umowy")) > 1 or len(_children(terms, "Zamowienia")) > 1:
        add("kolejne numery umów lub zamówień")
    return gaps

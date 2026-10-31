"""Profil użytkownika (profile.toml): dane firmy, rachunki, kontrahenci, katalog pozycji i ustawienia faktur."""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass, field, fields, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .codes import EU_COUNTRIES, GTU_CODES, MARGIN_PROCEDURES, PAYMENT_FORM_KEYS, THIRD_PARTY_ROLES, VAT_RATES
from .config import DATA_ROOT, ENVIRONMENTS, os_error_text
from .money import nip_valid, parse_amount

SYNC_SUBJECTS = {
    "wystawione": ("Subject1", "issued"),
    "otrzymane": ("Subject2", "received"),
    "podmiot3": ("Subject3", "third"),
    "upowaznione": ("SubjectAuthorized", "authorized"),
}
CONTEXT_TYPES = {"nip": "Nip", "id_wewnetrzny": "InternalId", "nip_vat_ue": "NipVatUe", "peppol": "PeppolId"}
AUTH_METHODS = ("token", "certyfikat")
DATE_MODES = ("dzisiaj", "koniec_miesiaca", "dzisiaj_za_miesiac")
NUMBER_TOKENS = ("nr", "m", "mm", "rrrr", "rr")


class ProfileError(Exception):
    pass


# ------------------------------------------------------------------- model


@dataclass(frozen=True)
class Party:
    """Sprzedawca, nabywca albo podmiot trzeci. Nazwy pól odpowiadają kluczom w profile.toml."""

    nazwa: str = ""
    nip: str = ""
    adres_l1: str = ""
    adres_l2: str = ""
    kraj: str = "PL"
    vat_ue: str = ""  # numer VAT UE z prefiksem kraju, np. DE123456789
    nr_id: str = ""  # inny identyfikator podatkowy (kraj nadania w polu `kraj`)
    brak_id: bool = False  # osoba prywatna lub podmiot bez identyfikatora podatkowego
    email: str = ""
    telefon: str = ""
    nr_klienta: str = ""
    jst: bool = False  # jednostka podrzędna JST
    gv: bool = False  # członek grupy VAT

    @property
    def id_label(self) -> str:
        if self.nip:
            return "NIP"
        if self.vat_ue:
            return "VAT UE"
        if self.nr_id:
            return "ID"
        return ""

    @property
    def identifier(self) -> str:
        if self.nip:
            return self.nip
        if self.vat_ue:
            return self.vat_ue
        if self.nr_id:
            return f"{self.kraj} {self.nr_id}"
        return ""

    @property
    def key(self) -> str:
        """Klucz do porównywania kontrahentów (identyfikator albo nazwa)."""
        return re.sub(r"\s+", "", self.identifier).upper() or self.nazwa.strip().lower()


@dataclass(frozen=True)
class Registers:
    krs: str = ""
    regon: str = ""
    bdo: str = ""

    def __bool__(self) -> bool:
        return bool(self.krs or self.regon or self.bdo)


@dataclass(frozen=True)
class BankAccount:
    numer: str
    bank: str = ""
    swift: str = ""
    opis: str = ""
    waluta: str = ""  # rachunek domyślny dla faktur w tej walucie (np. EUR)

    @property
    def number(self) -> str:
        return "".join(self.numer.split()).upper()


@dataclass(frozen=True)
class ThirdParty:
    party: Party = field(default_factory=Party)
    rola: int | None = None  # TRolaPodmiotu3; None = inna rola opisana w `opis_roli`
    opis_roli: str = ""
    udzial: Decimal | None = None  # udział procentowy dodatkowego nabywcy

    @property
    def role_label(self) -> str:
        return THIRD_PARTY_ROLES.get(self.rola or 0, self.opis_roli or "Inna rola")


@dataclass(frozen=True)
class Contractor:
    party: Party = field(default_factory=Party)
    domyslny: bool = False
    termin_platnosci_dni: int | None = None
    pozycja: str = ""  # domyślna pozycja z katalogu
    waluta: str = ""
    powiazany: bool = False  # powiązania między nabywcą a sprzedawcą (znacznik TP)
    podmioty3: tuple[ThirdParty, ...] = ()


@dataclass(frozen=True)
class CatalogItem:
    nazwa: str
    jednostka: str = "szt."
    stawka_vat: str = "23"
    cena_netto: Decimal | None = None
    domyslna: bool = False
    gtu: str = ""
    pkwiu: str = ""
    cn: str = ""
    indeks: str = ""
    zal15: bool = False  # towar/usługa z załącznika nr 15 (podzielona płatność)


@dataclass(frozen=True)
class InvoiceSettings:
    numeracja: str = "FV/{nr}/{mm}/{rrrr}"
    numeracja_korekt: str = "KOR/{nr}/{mm}/{rrrr}"
    daty: str = "dzisiaj"
    dzien_graniczny: int = 0
    termin_platnosci_dni: int = 14
    forma_platnosci: str = "przelew"
    waluta: str = "PLN"
    miejsce_wystawienia: str = ""
    metoda_kasowa: bool = False
    mpp: bool = False
    podstawa_zwolnienia: str = ""
    procedura_marzy: str = ""
    stopka: str = ""

    @property
    def payment_form(self) -> str:
        return PAYMENT_FORM_KEYS.get(self.forma_platnosci, "")


@dataclass(frozen=True)
class AuthSettings:
    metoda: str = "token"
    kontekst: str = "nip"
    identyfikator: str = ""  # identyfikator kontekstu, gdy inny niż NIP sprzedawcy
    certyfikat: str = ""
    klucz: str = ""
    identyfikacja: str = "podmiot"  # podmiot (certificateSubject) | odcisk (certificateFingerprint)
    dozwolone_ip: tuple[str, ...] = ()
    weryfikuj_certyfikat: bool = False

    @property
    def certificate_path(self) -> Path | None:
        return Path(self.certyfikat).expanduser() if self.certyfikat else None

    @property
    def key_path(self) -> Path | None:
        return Path(self.klucz).expanduser() if self.klucz else None


@dataclass(frozen=True)
class Profile:
    path: Path
    env: str
    seller: Party
    registers: Registers = field(default_factory=Registers)
    accounts: tuple[BankAccount, ...] = ()
    contractors: tuple[Contractor, ...] = ()
    items: tuple[CatalogItem, ...] = ()
    invoice: InvoiceSettings = field(default_factory=InvoiceSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    sync_since: date = field(default_factory=lambda: date(date.today().year, 1, 1))
    sync: tuple[str, ...] = ("wystawione", "otrzymane")
    theme: str = "textual-dark"

    @property
    def api_url(self) -> str:
        return ENVIRONMENTS[self.env]["api"]

    @property
    def qr_url(self) -> str:
        return ENVIRONMENTS[self.env]["qr"]

    @property
    def env_label(self) -> str:
        return ENVIRONMENTS[self.env]["label"]

    @property
    def context_type(self) -> str:
        return CONTEXT_TYPES[self.auth.kontekst]

    @property
    def context_id(self) -> str:
        return self.auth.identyfikator or self.seller.nip

    @property
    def data_dir(self) -> Path:
        return DATA_ROOT / self.env / re.sub(r"[^\w.-]+", "_", self.context_id)

    @property
    def directions(self) -> list[str]:
        """Kierunki faktur pobieranych z KSeF (issued, received, third, authorized)."""
        return [SYNC_SUBJECTS[key][1] for key in self.sync]

    @property
    def default_contractor(self) -> Contractor | None:
        return next((c for c in self.contractors if c.domyslny), None)

    def default_item(self, contractor: Contractor | None = None) -> CatalogItem | None:
        if contractor and contractor.pozycja and (item := self.item(contractor.pozycja)):
            return item
        return next((i for i in self.items if i.domyslna), self.items[0] if self.items else None)

    def item(self, name: str) -> CatalogItem | None:
        return next((i for i in self.items if i.nazwa.strip().lower() == name.strip().lower()), None)

    def contractor(self, party: Party) -> Contractor | None:
        return next((c for c in self.contractors if c.party.key == party.key), None)

    def with_env(self, env: str | None) -> Profile:
        if not env or env == self.env:
            return self
        if env not in ENVIRONMENTS:
            raise ProfileError(f"Nieznane środowisko {env!r}. Dozwolone: {', '.join(ENVIRONMENTS)}.")
        return replace(self, env=env)


# ------------------------------------------------------------------ odczyt


def _type_name(cls: type, name: str) -> str:
    return next(f.type for f in fields(cls) if f.name == name)


def _coerce(value: Any, type_name: str, where: str) -> Any:
    if type_name == "str":
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise ProfileError(f"{where}: oczekiwano tekstu w cudzysłowie.")
        return str(value).strip()
    if type_name == "bool":
        if not isinstance(value, bool):
            raise ProfileError(f"{where}: oczekiwano true albo false.")
        return value
    if type_name in ("int", "int | None"):
        if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
            raise ProfileError(f"{where}: oczekiwano liczby całkowitej.")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ProfileError(f"{where}: oczekiwano liczby całkowitej.") from None
    if type_name == "Decimal | None":
        amount = parse_amount(str(value)) if not isinstance(value, bool) else None
        if amount is None:
            raise ProfileError(f'{where}: oczekiwano kwoty, np. "1 500,00".')
        return amount
    if type_name == "tuple[str, ...]":
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ProfileError(f'{where}: oczekiwano listy tekstów, np. ["a", "b"].')
        return tuple(v.strip() for v in value)
    raise AssertionError(type_name)


def _check_keys(data: dict[str, Any], allowed: set[str], where: str) -> None:
    for key in data:
        if key not in allowed:
            hint = difflib.get_close_matches(key, sorted(allowed), n=1)
            suggestion = f" Czy chodziło o „{hint[0]}”?" if hint else f" Dozwolone: {', '.join(sorted(allowed))}."
            raise ProfileError(f"{where}: nieznane pole „{key}”.{suggestion}")


def _build(cls: type, data: Any, where: str, *, skip: tuple[str, ...] = ()) -> Any:
    if not isinstance(data, dict):
        raise ProfileError(f"{where}: oczekiwano sekcji z polami.")
    names = {f.name for f in fields(cls)} - set(skip)
    _check_keys(data, names, where)
    kwargs = {k: _coerce(v, _type_name(cls, k), f"{where} → {k}") for k, v in data.items() if k not in skip}
    return cls(**kwargs)


def _table_list(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = raw.get(key, [])
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ProfileError(f"Sekcje [[{key}]] mają niepoprawny format.")
    return value


CONTRACTOR_KEYS = {f.name for f in fields(Contractor)} - {"party"}
SELLER_KEYS = ("nazwa", "nip", "adres_l1", "adres_l2", "kraj", "email", "telefon")
THIRD_PARTY_KEYS = {"rola", "opis_roli", "udzial"}


def _party(data: dict[str, Any], where: str, extra: set[str] = frozenset()) -> Party:
    party_data = {k: v for k, v in data.items() if k not in extra}
    party = _build(Party, party_data, where)
    party = replace(party, kraj=(party.kraj or "PL").upper(), vat_ue=party.vat_ue.replace(" ", "").upper())
    if party.nip:
        party = replace(party, nip=re.sub(r"[\s-]", "", party.nip))
        if not nip_valid(party.nip):
            raise ProfileError(f"{where}: NIP {party.nip} jest niepoprawny (zła suma kontrolna).")
    if party.vat_ue and party.vat_ue[:2] not in EU_COUNTRIES:
        raise ProfileError(f"{where}: numer VAT UE musi zaczynać się kodem kraju UE (np. DE123456789).")
    return party


def _validate_rate(rate: str, where: str) -> str:
    rate = str(rate).strip()
    if rate not in VAT_RATES:
        raise ProfileError(f"{where}: nieznana stawka VAT {rate!r}. Dozwolone: {', '.join(VAT_RATES)}.")
    return rate


def validate_number_pattern(pattern: str) -> str | None:
    """Zwraca opis błędu wzoru numeracji albo None, gdy wzór jest poprawny."""
    tokens = re.findall(r"\{([^}]*)\}", pattern)
    if re.search(r"[{}]", re.sub(r"\{[^{}]*\}", "", pattern)):
        return "wzór zawiera niedomknięte nawiasy"
    if not any(t.split(":")[0] == "nr" for t in tokens):
        return "wzór musi zawierać {nr} (kolejny numer)"
    if sum(t.split(":")[0] == "nr" for t in tokens) != 1:
        return "wzór musi zawierać dokładnie jeden licznik {nr}"
    for token in tokens:
        name, colon, width = token.partition(":")
        if name not in NUMBER_TOKENS or (colon and not (name == "nr" and width.isdigit())):
            return f"nieznany element {{{token}}} – dozwolone: {{nr}}, {{nr:3}}, {{m}}, {{mm}}, {{rrrr}}, {{rr}}"
    return None


def parse_profile(raw: dict[str, Any], path: Path) -> Profile:
    top_keys = {
        "srodowisko",
        "synchronizuj_od",
        "synchronizuj",
        "motyw",
        "logowanie",
        "sprzedawca",
        "rachunki",
        "faktura",
        "kontrahenci",
        "pozycje",
    }
    _check_keys(raw, top_keys, "Profil")

    env = str(raw.get("srodowisko", "prod"))
    if env not in ENVIRONMENTS:
        raise ProfileError(f"srodowisko: nieznane środowisko {env!r}. Dozwolone: {', '.join(ENVIRONMENTS)}.")

    since_raw = raw.get("synchronizuj_od", date(date.today().year, 1, 1))
    try:
        sync_since = since_raw if isinstance(since_raw, date) else date.fromisoformat(str(since_raw))
    except ValueError:
        raise ProfileError("synchronizuj_od: oczekiwano daty RRRR-MM-DD.") from None

    sync = _coerce(raw.get("synchronizuj", ["wystawione", "otrzymane"]), "tuple[str, ...]", "synchronizuj")
    for key in sync:
        if key not in SYNC_SUBJECTS:
            raise ProfileError(f"synchronizuj: nieznana wartość {key!r}. Dozwolone: {', '.join(SYNC_SUBJECTS)}.")
    if not sync:
        raise ProfileError('synchronizuj: podaj co najmniej jeden rodzaj faktur, np. ["wystawione"].')

    if "sprzedawca" not in raw:
        raise ProfileError("Brak sekcji [sprzedawca] z danymi Twojej firmy.")
    if not isinstance(raw["sprzedawca"], dict):
        raise ProfileError("[sprzedawca]: oczekiwano sekcji z polami.")
    seller_raw = dict(raw["sprzedawca"])
    registers_raw = {k: seller_raw.pop(k) for k in ("krs", "regon", "bdo") if k in seller_raw}
    registers = _build(Registers, registers_raw, "[sprzedawca]")
    _check_keys(seller_raw, set(SELLER_KEYS) | {"krs", "regon", "bdo"}, "[sprzedawca]")
    seller = _party(seller_raw, "[sprzedawca]")
    if not seller.nip:
        raise ProfileError("[sprzedawca]: podaj NIP firmy.")
    if not seller.nazwa or not seller.adres_l1:
        raise ProfileError("[sprzedawca]: podaj nazwę firmy i adres (adres_l1).")
    if registers.krs and not re.fullmatch(r"\d{10}", registers.krs):
        raise ProfileError("[sprzedawca] → krs: numer KRS ma 10 cyfr.")

    accounts = []
    for n, data in enumerate(_table_list(raw, "rachunki"), 1):
        account = _build(BankAccount, data, f"[[rachunki]] nr {n}") if "numer" in data else None
        if account is None:
            raise ProfileError(f"[[rachunki]] nr {n}: podaj numer rachunku.")
        if not 10 <= len(account.number) <= 34:
            raise ProfileError(f"[[rachunki]] nr {n}: numer rachunku powinien mieć od 10 do 34 znaków.")
        accounts.append(replace(account, waluta=account.waluta.upper()))

    auth = _build(AuthSettings, raw.get("logowanie", {}), "[logowanie]")
    if auth.metoda not in AUTH_METHODS:
        raise ProfileError(f"[logowanie] → metoda: dozwolone {', '.join(AUTH_METHODS)}.")
    if auth.kontekst not in CONTEXT_TYPES:
        raise ProfileError(f"[logowanie] → kontekst: dozwolone {', '.join(CONTEXT_TYPES)}.")
    if auth.kontekst != "nip" and not auth.identyfikator:
        raise ProfileError("[logowanie] → identyfikator: podaj identyfikator kontekstu logowania.")
    if auth.identyfikacja not in ("podmiot", "odcisk"):
        raise ProfileError("[logowanie] → identyfikacja: dozwolone „podmiot” albo „odcisk”.")
    if auth.metoda == "certyfikat" and not auth.certyfikat:
        raise ProfileError("[logowanie] → certyfikat: podaj ścieżkę do pliku certyfikatu (.pem albo .p12).")

    invoice = _build(InvoiceSettings, raw.get("faktura", {}), "[faktura]")
    for key in ("numeracja", "numeracja_korekt"):
        if problem := validate_number_pattern(getattr(invoice, key)):
            raise ProfileError(f"[faktura] → {key}: {problem}.")
    if invoice.daty not in DATE_MODES:
        raise ProfileError(f"[faktura] → daty: dozwolone {', '.join(DATE_MODES)}.")
    if not 0 <= invoice.dzien_graniczny <= 28:
        raise ProfileError("[faktura] → dzien_graniczny: podaj dzień od 0 do 28.")
    if invoice.forma_platnosci and invoice.forma_platnosci not in PAYMENT_FORM_KEYS:
        raise ProfileError(f"[faktura] → forma_platnosci: dozwolone {', '.join(PAYMENT_FORM_KEYS)}.")
    if not re.fullmatch(r"[A-Z]{3}", invoice.waluta.upper()):
        raise ProfileError("[faktura] → waluta: podaj trzyliterowy kod waluty, np. PLN, EUR.")
    invoice = replace(invoice, waluta=invoice.waluta.upper())
    if invoice.procedura_marzy and invoice.procedura_marzy not in MARGIN_PROCEDURES:
        raise ProfileError(f"[faktura] → procedura_marzy: dozwolone {', '.join(MARGIN_PROCEDURES)}.")

    items = []
    for n, data in enumerate(_table_list(raw, "pozycje"), 1):
        where = f"[[pozycje]] nr {n}"
        if "stawka_vat" in data:
            data = {**data, "stawka_vat": _validate_rate(data["stawka_vat"], f"{where} → stawka_vat")}
        if not str(data.get("nazwa", "")).strip():
            raise ProfileError(f"{where}: podaj nazwę pozycji.")
        item = _build(CatalogItem, data, where)
        if item.gtu and item.gtu.upper() not in GTU_CODES:
            raise ProfileError(f"{where} → gtu: dozwolone GTU_01…GTU_13.")
        items.append(replace(item, gtu=item.gtu.upper()))

    contractors = []
    for n, data in enumerate(_table_list(raw, "kontrahenci"), 1):
        where = f"[[kontrahenci]] nr {n}" + (f" („{data['nazwa']}”)" if data.get("nazwa") else "")
        party = _party(data, where, extra=CONTRACTOR_KEYS)
        if not party.nazwa:
            raise ProfileError(f"{where}: podaj nazwę kontrahenta.")
        settings = {k: v for k, v in data.items() if k in CONTRACTOR_KEYS and k != "podmioty3"}
        contractor = _build(Contractor, settings, where, skip=("party", "podmioty3"))
        third = []
        for m, tp in enumerate(_table_list(data, "podmioty3"), 1):
            tp_where = f"{where} → [[kontrahenci.podmioty3]] nr {m}"
            tp_party = _party(tp, tp_where, extra=THIRD_PARTY_KEYS)
            role = tp.get("rola")
            if role is not None and role not in THIRD_PARTY_ROLES:
                raise ProfileError(f"{tp_where} → rola: podaj numer od 1 do 11 albo opis_roli.")
            udzial = _coerce(tp["udzial"], "Decimal | None", f"{tp_where} → udzial") if "udzial" in tp else None
            third.append(ThirdParty(tp_party, rola=role, opis_roli=str(tp.get("opis_roli", "")), udzial=udzial))
        contractors.append(replace(contractor, party=party, podmioty3=tuple(third), waluta=contractor.waluta.upper()))

    return Profile(
        path=path,
        env=env,
        seller=seller,
        registers=registers,
        accounts=tuple(accounts),
        contractors=tuple(contractors),
        items=tuple(items),
        invoice=invoice,
        auth=auth,
        sync_since=sync_since,
        sync=sync,
        theme=str(raw.get("motyw", "textual-dark")),
    )


def load_profile(path: Path, env_override: str | None = None) -> Profile:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"Błąd składni w pliku {path}: {exc}") from exc
    except OSError as exc:
        raise ProfileError(f"Nie można odczytać pliku {path}: {os_error_text(exc)}") from exc
    try:
        return parse_profile(raw, path).with_env(env_override)
    except ProfileError as exc:
        raise ProfileError(f"Błąd w profilu {path}:\n  {exc}") from None


# ------------------------------------------------------------------- zapis


def toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = re.sub(r"[\x00-\x1f\x7f]", lambda m: f"\\u{ord(m.group()):04x}", escaped)
    return f'"{escaped}"'


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return toml_str(f"{value:.2f}".replace(".", ","))
    if isinstance(value, tuple | list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return toml_str(str(value))


def _lines(obj: Any, defaults: Any, *, always: tuple[str, ...] = (), skip: tuple[str, ...] = ()) -> list[str]:
    """Pola różne od domyślnych (oraz `always`) jako linie `klucz = wartość`."""
    out = []
    for f in fields(obj):
        if f.name in skip:
            continue
        value = getattr(obj, f.name)
        if f.name in always or value != getattr(defaults, f.name):
            out.append(f"{f.name} = {_toml_value(value)}")
    return out


def render_party(party: Party, *, always: tuple[str, ...] = ("nazwa", "adres_l1", "adres_l2")) -> list[str]:
    id_fields = ("nip",) if party.nip else ("vat_ue",) if party.vat_ue else ("nr_id",) if party.nr_id else ()
    order = ["nazwa", "nip", "vat_ue", "nr_id", "brak_id", "adres_l1", "adres_l2", "kraj"]
    lines = _lines(party, Party(), always=always + id_fields)
    return sorted(lines, key=lambda line: order.index(k) if (k := line.split(" ", 1)[0]) in order else 99)


def render_contractor(contractor: Contractor) -> str:
    lines = ["[[kontrahenci]]", *render_party(contractor.party)]
    lines += _lines(contractor, Contractor(Party()), skip=("party", "podmioty3"))
    for tp in contractor.podmioty3:
        lines += ["", "[[kontrahenci.podmioty3]]", *render_party(tp.party)]
        if tp.rola is not None:
            lines.append(f"rola = {tp.rola}")
        if tp.opis_roli:
            lines.append(f"opis_roli = {toml_str(tp.opis_roli)}")
        if tp.udzial is not None:
            lines.append(f"udzial = {_toml_value(tp.udzial)}")
    return "\n".join(lines) + "\n"


def render_item(item: CatalogItem) -> str:
    lines = ["[[pozycje]]", *_lines(item, CatalogItem(""), always=("nazwa", "jednostka", "stawka_vat"))]
    return "\n".join(lines) + "\n"


def render_profile(profile: Profile) -> str:
    """Pełny, skomentowany profile.toml (kreator pierwszego uruchomienia)."""
    inv = profile.invoice
    out = [
        "# Profil ksefik: dane firmy i ustawienia wystawiania faktur.",
        "# Edycja z aplikacji (Ctrl+P → Edytuj profil) działa od razu. Opis pól: profile.example.toml w repozytorium.",
        "",
        '# Środowisko KSeF: "prod" (produkcja), "demo" (przedprodukcyjne) albo "test" (testowe).',
        "# Można je nadpisać przy uruchomieniu: ksefik --env test",
        f"srodowisko = {toml_str(profile.env)}",
        "",
        "# Od jakiej daty pobrać faktury przy pierwszej synchronizacji (RRRR-MM-DD).",
        f"synchronizuj_od = {toml_str(profile.sync_since.isoformat())}",
        '# Które faktury pobierać z KSeF: "wystawione", "otrzymane", "podmiot3" (jesteś na fakturze',
        '# podmiotem trzecim, np. płatnikiem lub odbiorcą) i "upowaznione" (podmiot upoważniony).',
        f"synchronizuj = {_toml_value(profile.sync)}",
        "",
        "[logowanie]",
        '# "token" - token KSeF (aplikacja zapyta o niego i zapisze w systemowym pęku kluczy),',
        '# "certyfikat" - certyfikat KSeF albo kwalifikowana pieczęć/podpis z plików PEM lub .p12.',
        f"metoda = {toml_str(profile.auth.metoda)}",
    ]
    auth_lines = _lines(profile.auth, AuthSettings(), skip=("metoda",))
    out += auth_lines or [
        '# certyfikat = "~/ksef/certyfikat.pem"',
        '# klucz = "~/ksef/klucz.pem"     # hasło do klucza: aplikacja zapyta o nie (albo KSEF_KEY_PASSWORD)',
        '# Kontekst logowania: "nip" (NIP sprzedawcy), "id_wewnetrzny", "nip_vat_ue" albo "peppol".',
        '# kontekst = "nip"',
        '# identyfikator = ""',
        '# dozwolone_ip = ["203.0.113.10", "10.0.0.0/8"]   # adresy, zakresy "a-b" lub maski CIDR',
    ]
    seller = profile.seller
    out += ["", "[sprzedawca]", *render_party(seller, always=("nazwa", "nip", "adres_l1", "adres_l2", "kraj"))]
    out += ['# email = ""'] * (not seller.email) + ['# telefon = ""'] * (not seller.telefon)
    out += _lines(profile.registers, Registers()) or [
        "# Numery w rejestrach pokazywane w stopce faktury:",
        '# krs = ""',
        '# regon = ""',
        '# bdo = ""',
    ]
    out += ["", "# Rachunki bankowe na fakturze. Pierwszy jest domyślny, w edycji faktury można wybrać inny."]
    if profile.accounts:
        for account in profile.accounts:
            out += ["[[rachunki]]", *_lines(account, BankAccount(""), always=("numer",)), ""]
    else:
        out += ["# [[rachunki]]", '# numer = "PL61 1090 1014 0000 0712 1981 2874"', '# bank = ""', ""]
    out += [
        "[faktura]",
        "# Numeracja: {nr} - kolejny numer, {nr:3} - z zerami (001), {m} / {mm} - miesiąc (9 / 09),",
        "# {rrrr} / {rr} - rok (2026 / 26). Licznik liczy się od 1 w każdym miesiącu, jeśli wzór zawiera",
        "# miesiąc, w każdym roku, jeśli zawiera tylko rok, a bez daty - bez końca.",
        f"numeracja = {toml_str(inv.numeracja)}",
        f"numeracja_korekt = {toml_str(inv.numeracja_korekt)}",
        "# Daty nowej faktury:",
        '#   "dzisiaj"            - wystawienie i wykonanie usługi: dzisiaj,',
        '#   "koniec_miesiaca"    - obie daty: ostatni dzień miesiąca, którego dotyczy faktura,',
        '#   "dzisiaj_za_miesiac" - wystawienie dzisiaj, wykonanie: ostatni dzień miesiąca, którego dotyczy.',
        f"daty = {toml_str(inv.daty)}",
        "# Do tego dnia miesiąca (włącznie) faktura dotyczy poprzedniego miesiąca; 0 - zawsze bieżącego.",
        f"dzien_graniczny = {inv.dzien_graniczny}",
        f"termin_platnosci_dni = {inv.termin_platnosci_dni}",
        "# gotowka | karta | bon | czek | kredyt | przelew | mobilna",
        f"forma_platnosci = {toml_str(inv.forma_platnosci)}",
        f"waluta = {toml_str(inv.waluta)}",
        "# Mechanizm podzielonej płatności (obowiązkowy powyżej 15 000 zł brutto dla pozycji z zał. 15).",
        f"mpp = {_toml_value(inv.mpp)}",
        "# Metoda kasowa (art. 21 ustawy o VAT).",
        f"metoda_kasowa = {_toml_value(inv.metoda_kasowa)}",
    ]
    shown = ("numeracja", "numeracja_korekt", "daty", "dzien_graniczny", "termin_platnosci_dni")
    shown += ("forma_platnosci", "waluta", "mpp", "metoda_kasowa")
    extra = _lines(inv, InvoiceSettings(), skip=shown)
    out += extra or [
        '# miejsce_wystawienia = "Warszawa"',
        '# Podstawa prawna zwolnienia dla pozycji ze stawką "zw", np. "art. 113 ust. 1 ustawy o VAT".',
        '# podstawa_zwolnienia = ""',
        "# Tekst w stopce każdej faktury.",
        '# stopka = ""',
    ]
    out += [
        "",
        "# Katalog pozycji. Pozycja z domyslna = true trafia na nową fakturę.",
        '# stawka_vat: "23", "8", "5", "0 KR", "0 WDT", "0 EX", "zw", "np I", "np II", "oo", "marza",',
        '# historyczne "22", "7" oraz ryczałt taxi "4", "3". Opcjonalnie: cena_netto, gtu, pkwiu, cn, indeks.',
    ]
    if profile.items:
        out += [render_item(item) for item in profile.items]
    else:
        out += ["# [[pozycje]]", '# nazwa = "Usługi"', '# jednostka = "usł."', '# stawka_vat = "23"', ""]
    out += [
        "# Kontrahenci. Kontrahent z domyslny = true jest podpowiadany przy nowej fakturze.",
        '# Nabywca z UE: vat_ue = "DE123456789" zamiast nip; spoza UE: nr_id = "..." i kraj = "US";',
        "# osoba prywatna bez NIP: brak_id = true. Opcjonalnie: email, telefon, nr_klienta,",
        "# termin_platnosci_dni, pozycja (domyślna pozycja z katalogu), waluta, jst, gv, powiazany.",
        "# Nowych kontrahentów możesz też zapisywać z edycji faktury (Ctrl+T).",
    ]
    if profile.contractors:
        out += [render_contractor(c) for c in profile.contractors]
    return "\n".join(out).rstrip() + "\n"


def append_block(path: Path, block: str) -> None:
    """Dopisuje sekcję na końcu profilu (bez naruszania komentarzy i układu pliku)."""
    text = path.read_text(encoding="utf-8")
    separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    path.write_text(text + separator + block, encoding="utf-8")


def set_top_level(path: Path, key: str, value: Any) -> None:
    """Ustawia klucz najwyższego poziomu (przed pierwszą sekcją [..]), zachowując resztę pliku."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    line = f"{key} = {_toml_value(value)}\n"
    header = next((i for i, text in enumerate(lines) if text.lstrip().startswith("[")), len(lines))
    for i in range(header):
        if re.match(rf"\s*{re.escape(key)}\s*=", lines[i]):
            lines[i] = line
            break
    else:
        at = header
        while at > 0 and not lines[at - 1].strip():
            at -= 1  # nad pustymi liniami oddzielającymi sekcję
        if at and not lines[at - 1].endswith("\n"):
            lines[at - 1] += "\n"
        lines.insert(at, line)
    path.write_text("".join(lines), encoding="utf-8")


def save_new_profile(profile: Profile) -> None:
    profile.path.parent.mkdir(parents=True, exist_ok=True)
    profile.path.write_text(render_profile(profile), encoding="utf-8")


def prepare_data_dir(profile: Profile) -> Path:
    """Katalog danych profilu (osobny dla środowiska i firmy)."""
    profile.data_dir.mkdir(parents=True, exist_ok=True)
    return profile.data_dir

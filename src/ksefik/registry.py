"""Dane firmy po NIP z wykazu podatników VAT („Biała lista” Ministerstwa Finansów).

Publiczne API bez klucza (limit ok. 100 zapytań dziennie z jednego adresu). Dane są zawsze produkcyjne -
niezależnie od środowiska KSeF - więc fikcyjnych firm z testowego KSeF tu nie ma.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

import httpx

WL_API = "https://wl-api.mf.gov.pl"
SOURCE = "wykaz podatników VAT (Biała lista MF)"

log = logging.getLogger("ksefik.registry")


class RegistryError(Exception):
    """Nie udało się pobrać danych - opis dla użytkownika."""


@dataclass(frozen=True)
class CompanyData:
    nip: str
    name: str
    address_l1: str  # ulica i numer (albo cały adres, gdy nie da się go podzielić)
    address_l2: str  # kod pocztowy i miejscowość
    status_vat: str  # Czynny | Zwolniony | Niezarejestrowany
    accounts: tuple[str, ...] = ()

    @property
    def vat_note(self) -> str:
        """Ostrzeżenie o statusie VAT innym niż „czynny” (pusty tekst, gdy wszystko w porządku)."""
        if self.status_vat == "Czynny":
            return ""
        if self.status_vat == "Zwolniony":
            return "Firma jest zwolniona z VAT (status w wykazie: „Zwolniony”)."
        return f"Firma nie jest czynnym podatnikiem VAT (status w wykazie: „{self.status_vat or 'brak'}”)."


# Formy prawne w skrócie dopuszczalnym w obrocie (KSH) - dłuższe najpierw, żeby „PROSTA SPÓŁKA AKCYJNA” nie
# stała się „PROSTA S.A.”, a „SPÓŁKA KOMANDYTOWO-AKCYJNA” - „SPÓŁKA KOMANDYTOWO-S.A.”.
LEGAL_FORMS = (
    ("SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ", "SP. Z O.O."),
    ("SPÓŁKA KOMANDYTOWO-AKCYJNA", "S.K.A."),
    ("PROSTA SPÓŁKA AKCYJNA", "P.S.A."),
    ("SPÓŁKA AKCYJNA", "S.A."),
    ("SPÓŁKA KOMANDYTOWA", "SP.K."),
    ("SPÓŁKA PARTNERSKA", "SP.P."),
    ("SPÓŁKA JAWNA", "SP.J."),
)


def short_name(name: str) -> str:
    """„ZIELONY OGRÓD SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ” → „ZIELONY OGRÓD SP. Z O.O.”."""
    name = " ".join(name.split())
    for full, short in LEGAL_FORMS:
        name = re.sub(rf"(?<!\w){full}(?!\w)", short, name, flags=re.IGNORECASE)
    return name


def split_address(address: str) -> tuple[str, str]:
    """„CHEMIKÓW 7, 09-411 PŁOCK” → („CHEMIKÓW 7”, „09-411 PŁOCK”); adres bez kodu pocztowego - cały w 1. linii."""
    address = " ".join(address.split())
    match = re.fullmatch(r"(.+?),\s*(\d{2}-\d{3}\s+.+)", address)
    return (match[1], match[2]) if match else (address, "")


async def find_company(
    nip: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 15.0
) -> CompanyData:
    """Nazwa, adres, status VAT i rachunki firmy o podanym NIP. Błąd - RegistryError z opisem po polsku."""
    url = f"{WL_API}/api/search/nip/{nip}"
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as http:
            response = await http.get(url, params={"date": date.today().isoformat()})
    except httpx.TimeoutException as exc:
        raise RegistryError("Wykaz podatników VAT nie odpowiedział na czas - spróbuj za chwilę.") from exc
    except httpx.HTTPError as exc:
        log.warning("GET %s -> %s", url, exc.__class__.__name__)
        raise RegistryError(f"Brak połączenia z wykazem podatników VAT ({exc.__class__.__name__}).") from exc
    log.debug("GET %s -> %s", url, response.status_code)
    if response.status_code == 429:
        raise RegistryError("Wyczerpano dzienny limit zapytań do wykazu podatników VAT - spróbuj jutro.")
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not response.is_success:
        detail = body.get("message") if isinstance(body, dict) else None
        raise RegistryError(f"Wykaz podatników VAT odrzucił zapytanie: {detail or f'HTTP {response.status_code}'}.")
    subject = (body.get("result") or {}).get("subject")
    if not subject:
        raise RegistryError(f"Nie ma firmy o NIP {nip} w wykazie podatników VAT - wpisz dane ręcznie.")
    # Siedziba (osoby prawne) albo miejsce prowadzenia działalności (osoby fizyczne - tu siedziby nie ma).
    l1, l2 = split_address(subject.get("residenceAddress") or subject.get("workingAddress") or "")
    return CompanyData(
        nip=subject.get("nip") or nip,
        name=short_name(subject.get("name") or ""),
        address_l1=l1,
        address_l2=l2,
        status_vat=subject.get("statusVat") or "",
        accounts=tuple(subject.get("accountNumbers") or ()),
    )

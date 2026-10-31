"""Słowniki schematu FA(3) i API KSeF: stawki VAT, formy płatności, rodzaje faktur, role, uprawnienia."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class VatRate:
    code: str  # wartość pola P_12 (dla "marza" pole P_12 nie występuje)
    label: str
    percent: Decimal | None  # None = bez kwoty podatku
    net_field: str
    vat_field: str | None = None
    vat_pln_field: str | None = None  # kwota VAT w PLN przy fakturze w walucie obcej

    @property
    def short(self) -> str:
        """Krótka etykieta do tabel: 23%, 0% WDT, zw., np. II, o.o."""
        if self.code.isdigit():
            return f"{self.code}%"
        return SHORT_RATE_LABELS.get(self.code, self.code)


SHORT_RATE_LABELS = {
    "0 KR": "0%",
    "0 WDT": "0% WDT",
    "0 EX": "0% EX",
    "zw": "zw.",
    "np I": "np. I",
    "np II": "np. II",
    "oo": "o.o.",
    "marza": "marża",
}


VAT_RATES: dict[str, VatRate] = {
    rate.code: rate
    for rate in (
        VatRate("23", "23%", Decimal(23), "P_13_1", "P_14_1", "P_14_1W"),
        VatRate("8", "8%", Decimal(8), "P_13_2", "P_14_2", "P_14_2W"),
        VatRate("5", "5%", Decimal(5), "P_13_3", "P_14_3", "P_14_3W"),
        VatRate("0 KR", "0% (kraj)", Decimal(0), "P_13_6_1"),
        VatRate("0 WDT", "0% WDT", Decimal(0), "P_13_6_2"),
        VatRate("0 EX", "0% eksport", Decimal(0), "P_13_6_3"),
        VatRate("zw", "zw. (zwolnione)", None, "P_13_7"),
        VatRate("np I", "np. (poza krajem)", None, "P_13_8"),
        VatRate("np II", "np. (usługi z art. 100 ust. 1 pkt 4)", None, "P_13_9"),
        VatRate("oo", "o.o. (odwrotne obciążenie)", None, "P_13_10"),
        VatRate("marza", "procedura marży", None, "P_13_11"),
        VatRate("22", "22%", Decimal(22), "P_13_1", "P_14_1", "P_14_1W"),
        VatRate("7", "7%", Decimal(7), "P_13_2", "P_14_2", "P_14_2W"),
        VatRate("4", "4% (ryczałt taxi)", Decimal(4), "P_13_4", "P_14_4", "P_14_4W"),
        VatRate("3", "3% (ryczałt taxi)", Decimal(3), "P_13_4", "P_14_4", "P_14_4W"),
    )
}

# Kolejność pól podsumowania w elemencie Fa (wymagana przez XSD).
SUMMARY_FIELDS = [
    "P_13_1", "P_14_1", "P_14_1W",
    "P_13_2", "P_14_2", "P_14_2W",
    "P_13_3", "P_14_3", "P_14_3W",
    "P_13_4", "P_14_4", "P_14_4W",
    "P_13_5", "P_14_5",
    "P_13_6_1", "P_13_6_2", "P_13_6_3", "P_13_7", "P_13_8", "P_13_9", "P_13_10", "P_13_11",
]  # fmt: skip

# Etykiety sum w podglądzie faktury: (pole netto, pole VAT, opis).
SUMMARY_LABELS: list[tuple[str, str | None, str]] = [
    ("P_13_1", "P_14_1", "23% lub 22%"),
    ("P_13_2", "P_14_2", "8% lub 7%"),
    ("P_13_3", "P_14_3", "5%"),
    ("P_13_4", "P_14_4", "4% lub 3%"),
    ("P_13_5", "P_14_5", "procedura OSS"),
    ("P_13_6_1", None, "0% (kraj)"),
    ("P_13_6_2", None, "0% WDT"),
    ("P_13_6_3", None, "0% eksport"),
    ("P_13_7", None, "zwolnione"),
    ("P_13_8", None, "np. (poza krajem)"),
    ("P_13_9", None, "np. (usługi art. 100)"),
    ("P_13_10", None, "odwrotne obciążenie"),
    ("P_13_11", None, "marża"),
]

PAYMENT_FORMS = {
    "1": "Gotówka",
    "2": "Karta",
    "3": "Bon",
    "4": "Czek",
    "5": "Kredyt",
    "6": "Przelew",
    "7": "Mobilna",
}
# Nazwy form płatności w profile.toml.
PAYMENT_FORM_KEYS = {
    "gotowka": "1",
    "karta": "2",
    "bon": "3",
    "czek": "4",
    "kredyt": "5",
    "przelew": "6",
    "mobilna": "7",
}

INVOICE_KINDS = {
    "VAT": "Faktura podstawowa",
    "KOR": "Faktura korygująca",
    "ZAL": "Faktura zaliczkowa",
    "ROZ": "Faktura rozliczeniowa",
    "UPR": "Faktura uproszczona",
    "KOR_ZAL": "Korekta faktury zaliczkowej",
    "KOR_ROZ": "Korekta faktury rozliczeniowej",
}

CORRECTION_TYPES = {
    "1": "W dacie ujęcia faktury pierwotnej",
    "2": "W dacie wystawienia korekty",
    "3": "W innej dacie",
}

THIRD_PARTY_ROLES = {
    1: "Faktor",
    2: "Odbiorca",
    3: "Podmiot pierwotny",
    4: "Dodatkowy nabywca",
    5: "Wystawca faktury",
    6: "Dokonujący płatności",
    7: "JST – wystawca",
    8: "JST – odbiorca",
    9: "Członek grupy VAT – wystawca",
    10: "Członek grupy VAT – odbiorca",
    11: "Pracownik",
}

MARGIN_PROCEDURES = {
    "turystyka": ("P_PMarzy_2", "Procedura marży dla biur podróży"),
    "towary_uzywane": ("P_PMarzy_3_1", "Procedura marży – towary używane"),
    "dziela_sztuki": ("P_PMarzy_3_2", "Procedura marży – dzieła sztuki"),
    "antyki": ("P_PMarzy_3_3", "Procedura marży – przedmioty kolekcjonerskie i antyki"),
}

GTU_CODES = {
    "GTU_01": "Napoje alkoholowe",
    "GTU_02": "Paliwa (art. 103 ust. 5aa)",
    "GTU_03": "Oleje opałowe i smarowe",
    "GTU_04": "Wyroby tytoniowe",
    "GTU_05": "Odpady",
    "GTU_06": "Urządzenia elektroniczne",
    "GTU_07": "Pojazdy i części",
    "GTU_08": "Metale szlachetne i nieszlachetne",
    "GTU_09": "Leki i wyroby medyczne",
    "GTU_10": "Budynki, budowle i grunty",
    "GTU_11": "Usługi – gazy cieplarniane",
    "GTU_12": "Usługi niematerialne (doradcze, księgowe, prawne, zarządcze, marketingowe…)",
    "GTU_13": "Usługi transportowe i magazynowe",
}

EU_COUNTRIES = [
    "AT",
    "BE",
    "BG",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "EL",
    "HR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
    "XI",
]

# ---------------------------------------------------------------------- API KSeF

PERMISSION_LABELS = {
    "InvoiceWrite": "Wystawianie faktur",
    "InvoiceRead": "Przeglądanie faktur",
    "CredentialsManage": "Zarządzanie uprawnieniami",
    "CredentialsRead": "Przeglądanie uprawnień",
    "Introspection": "Historia sesji i UPO",
    "SubunitManage": "Zarządzanie jednostkami podrzędnymi",
    "EnforcementOperations": "Operacje egzekucyjne",
    "VatUeManage": "Zarządzanie podmiotem unijnym",
    "CollectiveIdentifierManage": "Identyfikatory zbiorcze",
    "SelfInvoicing": "Samofakturowanie",
    "RRInvoicing": "Faktury VAT RR",
    "TaxRepresentative": "Przedstawiciel podatkowy",
    "PefInvoicing": "Faktury PEF (Peppol)",
    "PefInvoiceWrite": "Wysyłka faktur PEF",
}

TOKEN_PERMISSIONS = [
    "InvoiceRead",
    "InvoiceWrite",
    "CredentialsRead",
    "CredentialsManage",
    "SubunitManage",
    "EnforcementOperations",
    "Introspection",
    "CollectiveIdentifierManage",
]
PERSON_PERMISSIONS = [
    "InvoiceWrite",
    "InvoiceRead",
    "CredentialsManage",
    "CredentialsRead",
    "Introspection",
    "SubunitManage",
    "EnforcementOperations",
    "CollectiveIdentifierManage",
]
ENTITY_PERMISSIONS = ["InvoiceWrite", "InvoiceRead", "CollectiveIdentifierManage"]
AUTHORIZATION_PERMISSIONS = ["SelfInvoicing", "RRInvoicing", "TaxRepresentative", "PefInvoicing"]

TOKEN_STATUSES = {
    "Pending": "Oczekuje",
    "Active": "Aktywny",
    "Revoking": "Unieważniany",
    "Revoked": "Unieważniony",
    "Failed": "Błąd",
}

CERTIFICATE_STATUSES = {
    "Active": "Aktywny",
    "Blocked": "Zablokowany",
    "Revoked": "Unieważniony",
    "Expired": "Wygasł",
}
CERTIFICATE_TYPES = {"Authentication": "Uwierzytelnianie", "Offline": "Tryb offline"}
REVOCATION_REASONS = {
    "Unspecified": "Nieokreślony",
    "Superseded": "Zastąpiony nowym",
    "KeyCompromise": "Klucz prywatny ujawniony",
}

AUTH_METHODS = {
    "Token": "Token KSeF",
    "TrustedProfile": "Profil zaufany",
    "InternalCertificate": "Certyfikat KSeF",
    "QualifiedSignature": "Podpis kwalifikowany",
    "QualifiedSeal": "Pieczęć kwalifikowana",
    "PersonalSignature": "Podpis osobisty",
    "PeppolSignature": "Podpis dostawcy Peppol",
}

IDENTIFIER_TYPES = {
    "Nip": "NIP",
    "Pesel": "PESEL",
    "Fingerprint": "Odcisk certyfikatu",
    "InternalId": "Id. wewnętrzny",
    "NipVatUe": "NIP-VAT UE",
    "PeppolId": "Peppol ID",
    "AllPartners": "Wszyscy klienci",
    "System": "System KSeF",
    "VatUe": "VAT UE",
    "Other": "Inny",
    "None": "Brak",
}

ENTITY_ROLES = {
    "CourtBailiff": "Komornik sądowy",
    "EnforcementAuthority": "Organ egzekucyjny",
    "LocalGovernmentUnit": "Jednostka samorządu terytorialnego",
    "LocalGovernmentSubUnit": "Jednostka podrzędna JST",
    "VatGroupUnit": "Grupa VAT",
    "VatGroupSubUnit": "Członek grupy VAT",
}

SESSION_STATUSES = {
    "InProgress": "W toku",
    "Succeeded": "Zakończona",
    "Failed": "Błąd",
    "Cancelled": "Anulowana",
}

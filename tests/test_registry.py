"""Dane firmy po NIP z wykazu podatników VAT (Biała lista MF) - bez sieci, na MockTransport."""

import asyncio

import httpx
import pytest

from ksefik.registry import CompanyData, RegistryError, find_company, short_name, split_address

SUBJECT = {
    "name": "ORLEN  SPÓŁKA AKCYJNA",
    "nip": "7740001454",
    "statusVat": "Czynny",
    "residenceAddress": "CHEMIKÓW 7, 09-411 PŁOCK",
    "workingAddress": None,
    "accountNumbers": ["30102010260000140203550589", "38124052821111001080047440"],
}


def lookup(handler, nip="7740001454"):
    return asyncio.run(find_company(nip, transport=httpx.MockTransport(handler)))


def test_company_data_from_white_list():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json={"result": {"subject": SUBJECT, "requestId": "x"}})

    company = lookup(handler)
    assert seen[0].path == "/api/search/nip/7740001454" and seen[0].params["date"]
    assert company == CompanyData(
        nip="7740001454",
        name="ORLEN S.A.",
        address_l1="CHEMIKÓW 7",
        address_l2="09-411 PŁOCK",
        status_vat="Czynny",
        accounts=tuple(SUBJECT["accountNumbers"]),
    )
    assert company.vat_note == ""


def test_sole_proprietor_uses_working_address():
    subject = {**SUBJECT, "residenceAddress": None, "workingAddress": "UL. KRÓTKA 2/4, 90-001 ŁÓDŹ"}
    company = lookup(lambda r: httpx.Response(200, json={"result": {"subject": subject}}))
    assert (company.address_l1, company.address_l2) == ("UL. KRÓTKA 2/4", "90-001 ŁÓDŹ")


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("CHEMIKÓW 7, 09-411 PŁOCK", ("CHEMIKÓW 7", "09-411 PŁOCK")),
        ("UL. A, LOK. 5, 00-001  WARSZAWA", ("UL. A, LOK. 5", "00-001 WARSZAWA")),
        ("WIEŚ 12 62-100", ("WIEŚ 12 62-100", "")),
        ("", ("", "")),
    ],
)
def test_split_address(address, expected):
    assert split_address(address) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ZIELONY OGRÓD SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ", "ZIELONY OGRÓD SP. Z O.O."),
        ("X SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ SPÓŁKA KOMANDYTOWA", "X SP. Z O.O. SP.K."),
        ("Y SPÓŁKA KOMANDYTOWO-AKCYJNA", "Y S.K.A."),
        ("Z PROSTA SPÓŁKA AKCYJNA", "Z P.S.A."),
        ("KANCELARIA SPÓŁKA PARTNERSKA", "KANCELARIA SP.P."),
        ("KOWALSCY SPÓŁKA JAWNA", "KOWALSCY SP.J."),
        ("JAN NOWAK  USŁUGI", "JAN NOWAK USŁUGI"),
        ("SPÓŁKA AKCYJNAX", "SPÓŁKA AKCYJNAX"),  # tylko całe słowa
    ],
)
def test_legal_form_is_abbreviated(name, expected):
    assert short_name(name) == expected


@pytest.mark.parametrize(
    ("status", "note"), [("Zwolniony", "zwolniona z VAT"), ("Niezarejestrowany", "nie jest czynnym podatnikiem")]
)
def test_vat_status_note(status, note):
    company = lookup(lambda r: httpx.Response(200, json={"result": {"subject": {**SUBJECT, "statusVat": status}}}))
    assert note in company.vat_note


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(200, json={"result": {"subject": None}}), "Nie ma firmy o NIP 7740001454"),
        (httpx.Response(429, json={}), "limit zapytań"),
        (httpx.Response(400, json={"code": "WL-113", "message": "Nieprawidłowy NIP"}), "Nieprawidłowy NIP"),
        (httpx.Response(503, text="<html>"), "HTTP 503"),
    ],
)
def test_errors_are_explained(response, message):
    with pytest.raises(RegistryError, match=message):
        lookup(lambda r: response)


@pytest.mark.parametrize(
    ("error", "message"), [(httpx.ConnectError("x"), "Brak połączenia"), (httpx.ReadTimeout("x"), "na czas")]
)
def test_network_errors_are_explained(error, message):
    def handler(request):
        raise error

    with pytest.raises(RegistryError, match=message):
        lookup(handler)

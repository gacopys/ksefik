"""Biała lista MF na żywo (jedno zapytanie z dziennego limitu): format odpowiedzi nadal pasuje do aplikacji."""

import asyncio

import pytest

from ksefik.registry import RegistryError, find_company


def test_white_list_returns_name_address_and_status():
    company = asyncio.run(find_company("7740001454"))  # ORLEN S.A. - duża firma, która nie zniknie z wykazu
    assert "ORLEN" in company.name
    assert company.address_l1 and company.address_l2[:6].replace("-", "").isdigit()
    assert company.status_vat == "Czynny" and company.accounts


def test_white_list_unknown_nip_is_explained():
    with pytest.raises(RegistryError, match="Nie ma firmy"):
        asyncio.run(find_company("1111111111"))

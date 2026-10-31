import tomllib
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from ksefik.invoice import Line, Payment, check_draft, new_draft
from ksefik.profile import BankAccount, Party, ProfileError, load_profile, parse_profile, validate_number_pattern


@pytest.mark.parametrize(
    "section,field,value",
    [
        (None, "synchronizuj_od", "invalid"),
        (None, "synchronizuj", []),
        (None, "synchronizuj", ["unknown"]),
        (None, "synchronizuj", 123),
        (None, "sprzedawca", None),
        ("sprzedawca", "nip", ""),
        ("sprzedawca", "nazwa", ""),
        ("sprzedawca", "adres_l1", ""),
        ("sprzedawca", "krs", "123"),
        ("sprzedawca", "nazwa", []),
        ("logowanie", "metoda", "unknown"),
        ("logowanie", "kontekst", "unknown"),
        ("logowanie", "kontekst", "id_wewnetrzny"),
        ("logowanie", "identyfikacja", "unknown"),
        ("logowanie", "dozwolone_ip", [123]),
        ("faktura", "dzien_graniczny", -1),
        ("faktura", "dzien_graniczny", 29),
        ("faktura", "dzien_graniczny", True),
        ("faktura", "dzien_graniczny", "bad"),
        ("faktura", "waluta", "EURO"),
        ("faktura", "forma_platnosci", "unknown"),
        ("faktura", "procedura_marzy", "unknown"),
    ],
)
def test_invalid_profile_data_always_produces_profile_error(example_path, section, field, value):
    data = tomllib.loads(example_path.read_text())
    if section is None:
        if value is None:
            data.pop(field)
        else:
            data[field] = value
    else:
        data[section][field] = value
    with pytest.raises(ProfileError):
        parse_profile(data, example_path)


@pytest.mark.parametrize(
    "section,value",
    [
        ("sprzedawca", "not a table"),
        ("sprzedawca", 1),
        ("logowanie", []),
        ("rachunki", "not a list"),
        ("rachunki", [{}]),
        ("rachunki", [{"numer": "123"}]),
        ("pozycje", [{}]),
        ("pozycje", [{"nazwa": "Test", "gtu": "GTU_99"}]),
        ("pozycje", [{"nazwa": "Test", "cena_netto": "invalid"}]),
        ("kontrahenci", [{"nazwa": ""}]),
        ("kontrahenci", [{"nazwa": "Test", "podmioty3": [{"nazwa": "Third", "rola": 99}]}]),
    ],
)
def test_malformed_sections_are_reported_without_crashing(example_path, section, value):
    data = tomllib.loads(example_path.read_text())
    data[section] = value
    with pytest.raises(ProfileError):
        parse_profile(data, example_path)


@pytest.mark.parametrize("value", [1.5, "1.5", False, [], {}])
def test_integer_profile_settings_never_silently_truncate(example_path, value):
    data = tomllib.loads(example_path.read_text())
    data["faktura"]["termin_platnosci_dni"] = value
    with pytest.raises(ProfileError):
        parse_profile(data, example_path)


@pytest.mark.parametrize("pattern", ["FV/{nr}/{nr}", "FV/{nr}/{", "FV/{nr}/}", "FV/{nr:}/2026", "FV/{nr}/{unknown}"])
def test_bad_number_patterns_are_rejected_before_number_generation(pattern):
    assert validate_number_pattern(pattern) is not None


def test_profile_normalizes_types_country_currency_and_case(example_path):
    data = tomllib.loads(example_path.read_text())
    data["sprzedawca"]["nip"] = "543-853-95-02"
    data["faktura"]["waluta"] = "eur"
    data["faktura"]["termin_platnosci_dni"] = "30"
    data["logowanie"]["dozwolone_ip"] = " 127.0.0.1 "
    profile = parse_profile(data, example_path)
    assert profile.seller.nip == "5438539502"
    assert profile.invoice.waluta == "EUR" and profile.invoice.termin_platnosci_dni == 30
    assert profile.auth.dozwolone_ip == ("127.0.0.1",)


def test_missing_profile_is_readable(tmp_path):
    with pytest.raises(ProfileError, match="Nie można odczytać"):
        load_profile(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    "changes,fragment,blocking",
    [
        ({"number": " "}, "Brak numeru", True),
        ({"period": (date(2026, 10, 2), date(2026, 10, 1))}, "Początek okresu", True),
        ({"seller": Party(nip="123")}, "NIP sprzedawcy", True),
        ({"buyer": Party(nip="123")}, "NIP nabywcy", True),
        ({"buyer": Party(nip="5438539502", nazwa="Buyer", adres_l1="Address")}, "taki sam", False),
        ({"buyer": Party(nip="7163454949", nazwa="", adres_l1="")}, "nazwa nabywcy", True),
        ({"buyer": Party(nip="7163454949", nazwa="Buyer", adres_l1="Address", jst=True, gv=True)}, "JST", False),
        ({"lines": ()}, "żadnej pozycji", True),
        ({"lines": (Line("", unit_price=Decimal(100)),)}, "brak nazwy", True),
        ({"lines": (Line("Test", quantity=Decimal(-1), unit_price=Decimal(100)),)}, "ilość", True),
        ({"lines": (Line("Test", unit_price=Decimal(-1)),)}, "cena", True),
        ({"lines": (Line("Test", unit_price=Decimal(100), vat_rate="marza"),)}, "rodzaju procedury", True),
        ({"lines": (Line("Test", unit_price=Decimal(100), vat_rate="0 WDT"),)}, "VAT UE", False),
        (
            {
                "buyer": Party(nazwa="Buyer", brak_id=True, adres_l1="Address"),
                "lines": (Line("Test", unit_price=Decimal(100), vat_rate="oo"),),
            },
            "polskim NIP",
            False,
        ),
        ({"payment": Payment(paid=True)}, "datę zapłaty", True),
        ({"payment": Payment(due_date=date(2020, 1, 1))}, "wcześniejszy", False),
        ({"payment": Payment(form="6")}, "bez numeru rachunku", False),
        ({"payment": Payment(accounts=(BankAccount("123"),))}, "długość", True),
        ({"kind": "KOR"}, "numer faktury korygowanej", True),
    ],
)
def test_invoice_validation_explains_specific_problems(profile, changes, fragment, blocking):
    draft = new_draft(profile, date(2026, 10, 23), [], net=Decimal(100))
    issues = check_draft(replace(draft, **changes), date(2026, 10, 23))
    assert any(fragment in issue.message and issue.blocking is blocking for issue in issues)

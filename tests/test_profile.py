from dataclasses import replace
from decimal import Decimal

import pytest

from ksefik.profile import (
    Contractor,
    Party,
    ProfileError,
    append_block,
    load_profile,
    render_contractor,
    render_profile,
    save_new_profile,
    set_top_level,
)


def test_example_profile_loads(profile):
    assert profile.env == "prod"
    assert profile.seller.nip == "5438539502"
    assert profile.registers.regon == "123456785"
    assert [a.waluta for a in profile.accounts] == ["", "EUR"]
    assert profile.accounts[0].number == "61109010140000071219812874"  # spacje usunięte
    assert profile.default_contractor.party.nazwa == "Przykładowa Spółka z o.o."
    assert profile.default_item().nazwa == "Usługi programistyczne"
    eu = profile.contractors[1]
    assert eu.party.vat_ue == "DE123456789" and eu.waluta == "EUR"
    assert profile.default_item(eu).stawka_vat == "np II"  # pozycja przypisana do kontrahenta
    assert profile.items[1].cena_netto == Decimal("250.00")
    jst = profile.contractors[2]
    assert jst.party.jst and jst.podmioty3[0].rola == 8
    assert profile.directions == ["issued", "received"]
    assert profile.context_type == "Nip" and profile.context_id == "5438539502"


def test_env_override(example_path):
    assert load_profile(example_path, "test").api_url == "https://api-test.ksef.mf.gov.pl/v2"
    with pytest.raises(ProfileError):
        load_profile(example_path, "qa")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("termin_platnosci_dni = 14", "termin_platnosc_dni = 14"), "Czy chodziło o „termin_platnosci_dni”"),
        (('nip = "5438539502"', 'nip = "5438539503"'), "NIP 5438539503 jest niepoprawny"),
        (('numeracja = "FV/{nr}/{mm}/{rrrr}"', 'numeracja = "FV/{mm}/{rrrr}"'), "musi zawierać {nr}"),
        (('daty = "dzisiaj"', 'daty = "jutro"'), "daty: dozwolone"),
        (('stawka_vat = "np II"', 'stawka_vat = "19"'), "nieznana stawka VAT '19'"),
        (('srodowisko = "prod"', 'srodowisko = "qa"'), "nieznane środowisko"),
        (('vat_ue = "DE123456789"\nadres_l1', 'vat_ue = "US123"\nadres_l1'), "kodem kraju UE"),
        (('metoda = "token"', 'metoda = "certyfikat"'), "podaj ścieżkę do pliku certyfikatu"),
        (("mpp = false", 'mpp = "nie"'), "oczekiwano true albo false"),
    ],
)
def test_profile_errors_are_human(example_path, change, message):
    text = example_path.read_text(encoding="utf-8")
    assert change[0] in text
    example_path.write_text(text.replace(change[0], change[1], 1), encoding="utf-8")
    with pytest.raises(ProfileError, match=message.replace("{", r"\{").replace("}", r"\}").replace("(", r"\(")):
        load_profile(example_path)


def test_syntax_error(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text("srodowisko = prod\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="Błąd składni"):
        load_profile(path)


def test_render_roundtrip(profile, tmp_path):
    save_new_profile(replace(profile, path=tmp_path / "copy.toml"))
    again = load_profile(tmp_path / "copy.toml")
    for field in ("env", "seller", "registers", "accounts", "contractors", "items", "invoice", "auth", "sync"):
        assert getattr(again, field) == getattr(profile, field), field


def test_set_top_level_replaces_or_adds_the_key_before_sections(profile, tmp_path):
    set_top_level(profile.path, "motyw", "nord")
    text = profile.path.read_text(encoding="utf-8")
    assert load_profile(profile.path).theme == "nord" and text.count("motyw =") == 1
    assert "zmienisz też w palecie" in text  # komentarz nad kluczem zostaje
    save_new_profile(replace(profile, path=tmp_path / "bez.toml"))  # profil z kreatora nie ma klucza motyw
    set_top_level(tmp_path / "bez.toml", "motyw", "gruvbox")
    again = load_profile(tmp_path / "bez.toml")
    assert again.theme == "gruvbox" and again.auth == profile.auth


def test_append_contractor_keeps_file_valid(profile):
    buyer = Party(nazwa='Firma "Cudzysłów" & Syn', nip="4584450427", adres_l1="ul. Nowa 5", adres_l2="00-950 Warszawa")
    append_block(profile.path, render_contractor(Contractor(buyer)))
    again = load_profile(profile.path)
    assert again.contractors[-1].party == buyer
    assert "opis wszystkich ustawień" in profile.path.read_text(encoding="utf-8")  # komentarze zostają


def test_render_profile_contains_hints(profile):
    text = render_profile(profile)
    assert 'numeracja = "FV/{nr}/{mm}/{rrrr}"' in text
    assert "[[kontrahenci.podmioty3]]" in text

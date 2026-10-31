from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from ksefik.client import KsefError
from ksefik.money import fmt_account, fmt_amount, fmt_date, fmt_qty, nip_valid, parse_amount, round2
from ksefik.profile import Party
from ksefik.submissions import buyer_identifier
from ksefik.ui import common, permissions
from ksefik.ui.invoice_screens import verify_url
from ksefik.ui.main_screen import _ago, period_range
from ksefik.ui.widgets import amount_preview


@pytest.mark.parametrize(
    "kind,valid,invalid",
    [
        ("Nip", "5438539502", "123"),
        ("Pesel", "44051401458", "44051401459"),
        ("Fingerprint", "A" * 64, "Z" * 64),
    ],
)
def test_identifier_validation(kind, valid, invalid):
    check = common.check_identifier(kind)
    assert check(valid) is None
    assert check(invalid)
    assert common.check_pesel("123")
    assert common.check_description("abcd")
    assert common.check_description("x" * 257)
    assert common.check_description("valid") is None


def test_display_helpers_preserve_literal_user_text_and_format_identifiers():
    assert common.fmt_ident(None) == ""
    assert common.fmt_ident({"type": "Nip", "value": "123"}) == "NIP 123"
    assert common.fmt_ident({"type": "Fingerprint", "value": "A" * 64}).endswith("A" * 16 + "…")
    assert common.person_name({"subjectPersonDetails": {"firstName": "Jan", "lastName": "Test"}}) == "Jan Test"
    assert common.person_name({"subjectEntityDetails": {"fullName": "Company"}}) == "Company"
    assert common.person_name({}) == ""
    assert common.fmt_dt(None) == ""
    assert common.fmt_dt("2026-10-23T00:00:00Z")
    assert common.fmt_dt(datetime.now(UTC), seconds=True)
    error = common.error_text(KsefError("[bold]", http_status=403, details=["[link]"]), "Needs permission")
    assert r"\[bold]" in error and r"\[link]" in error and "Needs permission" in error


def test_download_paths_prefer_localized_directory_and_sanitize_name(tmp_path, monkeypatch):
    monkeypatch.setattr(common.Path, "home", lambda: tmp_path)
    assert common.downloads_dir() == tmp_path
    (tmp_path / "Downloads").mkdir()
    assert common.downloads_dir() == tmp_path / "Downloads"
    (tmp_path / "Pobrane").mkdir()
    assert common.downloads_dir() == tmp_path / "Pobrane"
    assert common.short_path(tmp_path / "file") == "~/file"
    assert common.short_path(tmp_path.parent / "different") == str(tmp_path.parent / "different")
    saved = common.save_download("../../FV / 1.xml", b"content")
    assert saved.ok and saved.path.parent == tmp_path / "Pobrane" and saved.path.read_bytes() == b"content"
    assert common.safe_name("///") == "plik"


@pytest.mark.parametrize(
    "key,start,end",
    [
        ("this_month", date(2026, 1, 1), date(2026, 1, 15)),
        ("prev_month", date(2025, 12, 1), date(2025, 12, 31)),
        ("last_3", date(2025, 11, 1), date(2026, 1, 15)),
        ("this_year", date(2026, 1, 1), date(2026, 12, 31)),
        ("prev_year", date(2025, 1, 1), date(2025, 12, 31)),
        ("all", None, None),
    ],
)
def test_period_ranges_cross_year_boundary(key, start, end):
    assert period_range(key, date(2026, 1, 15)) == (start, end)


def test_relative_sync_times():
    now = datetime.now(UTC)
    assert _ago(None) == "nigdy"
    assert _ago(now) == "przed chwilą"
    assert _ago(now - timedelta(minutes=5)) == "5 min temu"
    assert _ago(now - timedelta(hours=2)) == "2 godz. temu"
    assert "temu" not in _ago(now - timedelta(days=3))


@pytest.mark.parametrize(
    "party,expected",
    [
        (Party(nip="123"), {"type": "Nip", "value": "123"}),
        (Party(vat_ue="DE123"), {"type": "VatUe", "value": "DE123"}),
        (Party(nr_id="other"), {"type": "Other", "value": "other"}),
        (Party(brak_id=True), {"type": "None"}),
    ],
)
def test_buyer_identifier_metadata(party, expected):
    assert buyer_identifier(party) == expected


def test_qr_links_use_urlsafe_unpadded_hash():
    assert verify_url("https://qr.test", "123", None, "abcd") == ""
    assert verify_url("https://qr.test", "123", date(2026, 10, 23), None) == ""
    assert (
        verify_url("https://qr.test", "123", date(2026, 10, 23), "/+8=") == "https://qr.test/invoice/123/23-10-2026/_-8"
    )


@pytest.mark.parametrize("rate,expected", [("23", "123,00"), ("zw", "100,00"), ("unknown", "100,00")])
def test_amount_preview_computes_vat_or_displays_exemption(rate, expected):
    assert expected in amount_preview(Decimal(100), rate)
    assert "Wpisz kwotę" in amount_preview(None, rate)
    assert "Wpisz kwotę" in amount_preview(Decimal(-1), rate)


def test_money_rounding_and_empty_formatting():
    assert round2(Decimal("1.005")) == Decimal("1.01")
    assert round2(Decimal("-1.005")) == Decimal("-1.01")
    assert fmt_amount(None) == "" and fmt_date(None) == ""
    assert fmt_qty(Decimal("1.2500")) == "1,25"
    assert fmt_account("invalid") == "invalid"
    assert not nip_valid("abcdefghij")
    assert parse_amount("NaN") is None and parse_amount("Infinity") is None


def test_person_details_validation_and_fingerprint_payloads():
    values = {"kind": "Pesel", "value": "44051401458", "first": "Jan", "last": "Test", "permissions": []}
    assert permissions._validate_person(values)[0] == "permissions"
    values["permissions"] = ["InvoiceRead"]
    assert permissions._validate_person(values) is None
    assert permissions._subject_details(values)["personById"] == {"firstName": "Jan", "lastName": "Test"}
    values.update(kind="Fingerprint", value="A" * 64)
    assert permissions._validate_person(values)[0] == "birth"
    values.update(birth=date(1990, 1, 1), doc_type="passport", doc_number="123", doc_country="pl")
    assert permissions._validate_person(values) is None
    assert permissions._subject_details(values)["personByFpNoId"]["idDocument"]["country"] == "PL"
    values.update(fp_id_type="Pesel", fp_id="")
    assert permissions._validate_person(values)[0] == "fp_id"
    values["fp_id"] = "44051401458"
    assert permissions._subject_details(values)["personByFpWithId"]["identifier"]["value"] == "44051401458"
    values.update(kind="Pesel", value="bad")
    assert permissions._validate_person(values)[0] == "value"


def test_eu_permission_subjects_require_complete_identity():
    values = {"subject": "entity", "entity_name": "Company", "entity_address": "Street"}
    assert permissions.PermissionsScreen._validate_eu(values) is None
    assert permissions.PermissionsScreen._eu_subject(values)["entityByFp"]["fullName"] == "Company"
    values = {"subject": "person", "first": "Jan", "last": "Test", "person_id": "123", "person_id_type": "Pesel"}
    assert permissions.PermissionsScreen._validate_eu(values) == ("person_id", "PESEL ma 11 cyfr")
    values["person_id"] = "44051401458"
    assert permissions.PermissionsScreen._validate_eu(values) is None
    assert permissions.PermissionsScreen._validate_eu({**values, "context": "1234567890-DE1"})[0] == "context"
    assert permissions.PermissionsScreen._eu_subject(values)["personByFpWithId"]["identifier"]["type"] == "Pesel"
    values["first"] = ""
    assert permissions.PermissionsScreen._validate_eu(values)[0] == "first"
    values.update(first="Jan", permissions=[])
    assert permissions.PermissionsScreen._validate_eu(values)[0] == "permissions"

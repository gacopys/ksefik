"""Uprawnienia: wszystkie widoki i wszystkie rodzaje nadawania oraz odbieranie - przez formularze aplikacji."""

import asyncio
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from helpers import (
    choose,
    fill_form,
    running_app,
    set_value,
    wait_loaded,
    wait_notification,
    wait_screen,
    wait_until,
    write_profile,
)
from ksef_env import internal_id, random_nip, random_pesel
from test_05_account import open_section, select_row

from ksefik.ui.common import ConfirmScreen
from ksefik.ui.main_screen import MainScreen
from ksefik.ui.permissions import VIEWS, PermissionsScreen


def ids(screen) -> list[str]:
    return [
        (item.get("authorizedIdentifier") or item.get("authorizedEntityIdentifier") or {}).get("value", "")
        for item in screen.items
    ]


async def show_view(pilot, screen, view: str) -> None:
    screen.items = []  # żeby nie sprawdzać wierszy poprzedniego widoku, zanim wczytają się nowe
    set_value(screen, "#view", view)
    await pilot.pause(0.5)
    await wait_loaded(pilot, screen)


async def grant(pilot, kind_label: str, values: dict) -> None:
    await pilot.press("n")
    await choose(pilot, kind_label)
    await fill_form(pilot, values)
    await wait_notification(pilot, "Uprawnienia nadane")


def make_certificate(folder, name: str):
    """Certyfikat osoby z UE (atrybuty jak w kwalifikowanym) - bez PESEL/NIP, więc KSeF rozpoznaje go po odcisku."""
    key = ec.generate_private_key(ec.SECP256R1())
    first, _, last = name.partition(" ")
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.GIVEN_NAME, first),
            x509.NameAttribute(NameOID.SURNAME, last or first),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, f"PNODE-{random_nip()}"),
            x509.NameAttribute(NameOID.COMMON_NAME, name),
            x509.NameAttribute(NameOID.COUNTRY_NAME, "DE"),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=7))
        .sign(key, hashes.SHA256())
    )
    folder.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = folder / f"{name}.pem", folder / f"{name}.klucz.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path, cert.fingerprint(hashes.SHA256()).hex().upper()


def test_grant_and_revoke_all_kinds(isolated_app, company, partner, monkeypatch):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)
    person, admin, worker = random_pesel(), random_pesel(), random_pesel()
    office, self_billing = random_nip(), random_nip()
    _, _, eu_fingerprint = make_certificate(isolated_app / "ue", "Admin UE")

    async def delegate_from_partner():
        """Klient (partner) daje firmie uprawnienie z prawem dalszego przekazywania - podstawa uprawnień pośrednich."""
        async with partner.client() as client:
            ref = await client.grant_entity_permissions(
                company.nip, [("InvoiceRead", True)], "Biuro dla klienta", company.name[:90]
            )
            await client.wait_for_permission_operation(ref)

    async def scenario():
        await delegate_from_partner()
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            screen = await open_section(pilot, "Uprawnienia", PermissionsScreen)
            await wait_loaded(pilot, screen)

            # Każdy widok wczytuje się bez błędu (część jest pusta dla zwykłej firmy).
            for view in VIEWS:
                await show_view(pilot, screen, view)
            await show_view(pilot, screen, "entities")
            assert any(i["contextIdentifier"]["value"] == partner.nip for i in screen.items), (
                "brak uprawnienia od klienta"
            )

            # 1. Osoba fizyczna - dwa uprawnienia.
            await show_view(pilot, screen, "granted")
            await grant(
                pilot,
                "Osobie fizycznej",
                {
                    "kind": "Pesel",
                    "value": person,
                    "first": "Jan",
                    "last": "Testowy",
                    "permissions": ["InvoiceRead", "InvoiceWrite"],
                    "description": "Księgowa (test e2e)",
                },
            )
            await wait_until(pilot, lambda: ids(screen).count(person) == 2, "dwa uprawnienia osoby")

            # 2. Inna firma (biuro rachunkowe) z prawem dalszego przekazywania.
            await grant(
                pilot,
                "Innej firmie",
                {
                    "nip": office,
                    "name": "Biuro Rachunkowe Testowe",
                    "permissions": ["InvoiceRead"],
                    "delegate": True,
                    "description": "Biuro (test e2e)",
                },
            )
            await wait_until(pilot, lambda: office in ids(screen), "uprawnienie biura")

            # 3. Pośrednio - pracownik biura obsługuje klienta (partnera), który nam to delegował.
            await grant(
                pilot,
                "Pośrednio",
                {
                    "kind": "Pesel",
                    "value": worker,
                    "first": "Ewa",
                    "last": "Pracownik",
                    "permissions": ["InvoiceRead"],
                    "target": "Nip",
                    "target_value": partner.nip,
                    "description": "Pracownik dla klienta (test e2e)",
                },
            )
            await wait_until(pilot, lambda: worker in ids(screen), "uprawnienie pośrednie")

            # 4. Administrator nowej jednostki wewnętrznej.
            unit = internal_id(company.nip, 7)
            await grant(
                pilot,
                "Administrator jednostki",
                {
                    "kind": "Pesel",
                    "value": admin,
                    "first": "Adam",
                    "last": "Kierownik",
                    "context": "InternalId",
                    "context_value": unit,
                    "subunit": "Oddział testowy",
                    "description": "Admin oddziału (test e2e)",
                },
            )
            await show_view(pilot, screen, "subunits")
            assert any(i.get("subunitIdentifier", {}).get("value") == unit for i in screen.items)

            # 5. Uprawnienie podmiotowe (samofakturowanie) - nadanie i odebranie.
            await show_view(pilot, screen, "authorizations")
            await grant(
                pilot,
                "Uprawnienie podmiotowe",
                {
                    "kind": "Nip",
                    "value": self_billing,
                    "name": "Nabywca Samofakturujący",
                    "permission": "SelfInvoicing",
                    "description": "Samofakturowanie (test e2e)",
                },
            )
            await wait_until(pilot, lambda: self_billing in ids(screen), "uprawnienie podmiotowe")
            select_row(screen, lambda i: i["authorizedEntityIdentifier"]["value"] == self_billing)
            await pilot.press("delete")
            await wait_screen(pilot, ConfirmScreen)
            await pilot.click("#confirm")
            await wait_notification(pilot, "Uprawnienie odebrane")
            await wait_until(pilot, lambda: self_billing not in ids(screen), "odebranie uprawnienia podmiotowego")

            # 6. Administrator podmiotu unijnego (kontekst NIP-VAT UE).
            await grant(
                pilot,
                "Administrator podmiotu unijnego",
                {
                    "fingerprint": eu_fingerprint,
                    "subject": "entity",
                    "entity_name": "Beispiel GmbH",
                    "entity_address": "Hauptstraße 1, 10115 Berlin",
                    "context": f"{company.nip}-DE123456789",
                    "eu_name": "Beispiel GmbH",
                    "eu_address": "Hauptstraße 1, 10115 Berlin",
                    "description": "Administrator UE (test e2e)",
                },
            )
            await show_view(pilot, screen, "eu")
            assert any(i["authorizedFingerprintIdentifier"] == eu_fingerprint for i in screen.items)

            # 7. Odebranie uprawnień osoby (oba wpisy).
            await show_view(pilot, screen, "granted")
            for _ in range(2):
                select_row(screen, lambda i: i["authorizedIdentifier"]["value"] == person)
                await pilot.press("delete")
                await wait_screen(pilot, ConfirmScreen)
                await pilot.click("#confirm")
                await wait_until(pilot, lambda: ids(screen).count(person) < 2 or not screen.items, "odświeżenie listy")
                await pilot.pause(1)
            await wait_until(pilot, lambda: person not in ids(screen), "odebranie uprawnień osoby")

    asyncio.run(scenario())


def test_eu_context_login_and_representative(isolated_app, company, partner):
    """Administrator podmiotu z UE loguje się certyfikatem (po odcisku) w kontekście NIP-VAT UE
    i nadaje reprezentanta."""
    cert_path, key_path, fingerprint = make_certificate(isolated_app / "ue", "Administrator UE")
    _, _, representative = make_certificate(isolated_app / "ue", "Reprezentant UE")
    context = f"{company.nip}-DE987654321"

    async def grant_admin():
        async with company.client(token=True) as client:
            details = {"fullName": "Beispiel Zwei GmbH", "address": "Berlin"}
            subject = {"subjectDetailsType": "EntityByFingerprint", "entityByFp": details}
            ref = await client.grant_eu_entity_admin(
                fingerprint, context, "Administrator UE (test e2e)", "Beispiel Zwei GmbH, Berlin", subject, details
            )
            await client.wait_for_permission_operation(ref)

    auth = (
        'metoda = "certyfikat"\n'
        f'certyfikat = "{cert_path}"\n'
        f'klucz = "{key_path}"\n'
        'kontekst = "nip_vat_ue"\n'
        f'identyfikator = "{context}"\n'
        'identyfikacja = "odcisk"\n'
    )
    path = write_profile(isolated_app / "profile.toml", company, partner, auth=auth)

    async def scenario():
        await grant_admin()
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            screen = await open_section(pilot, "Uprawnienia", PermissionsScreen)
            await wait_loaded(pilot, screen)
            await show_view(pilot, screen, "personal")
            assert any(i["permissionScope"] == "VatUeManage" for i in screen.items)
            await grant(
                pilot,
                "Reprezentant podmiotu unijnego",
                {
                    "fingerprint": representative,
                    "subject": "entity",
                    "entity_name": "Vertreter GmbH",
                    "entity_address": "Berlin",
                    "permissions": ["InvoiceRead", "InvoiceWrite"],
                    "description": "Reprezentant UE (test e2e)",
                },
            )
            await show_view(pilot, screen, "eu")
            assert any(i["authorizedFingerprintIdentifier"] == representative for i in screen.items)

    asyncio.run(scenario())

"""„Pobierz dane” przy NIP: nazwa i adres z wykazu podatników VAT w kreatorze profilu i w edycji nabywcy."""

import asyncio
from datetime import date
from decimal import Decimal

from textual.widgets import Button, Input, Select

from ksefik import registry
from ksefik.invoice import new_draft
from ksefik.profile import Party
from ksefik.registry import CompanyData, RegistryError
from ksefik.ui.common import FormScreen
from ksefik.ui.editor import EditScreen
from ksefik.ui.setup_screen import profile_wizard

ORLEN = CompanyData(
    nip="7740001454",
    name="ORLEN SPÓŁKA AKCYJNA",
    address_l1="CHEMIKÓW 7",
    address_l2="09-411 PŁOCK",
    status_vat="Czynny",
    accounts=("30102010260000140203550589",),
)


def fake_registry(monkeypatch, result):
    asked = []

    async def find_company(nip):
        asked.append(nip)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(registry, "find_company", find_company)
    return asked


def text(screen, selector) -> str:
    return str(screen.query_one(selector).render())


def test_wizard_fills_company_and_carries_the_only_account(seeded, monkeypatch):
    app, _, _ = seeded
    asked = fake_registry(monkeypatch, ORLEN)

    async def scenario():
        results = []
        async with app.run_test(size=(80, 24)) as pilot:
            form = profile_wizard(app.profile_path)
            await app.push_screen(form, results.append)
            await pilot.pause()
            form.query_one("#f-nip", Input).value = "7740001454"
            form.query_one("#act-nip", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert asked == ["7740001454"]
            assert form.query_one("#f-name", Input).value == "ORLEN SPÓŁKA AKCYJNA"
            assert form.query_one("#f-l1", Input).value == "CHEMIKÓW 7"
            assert form.query_one("#f-l2", Input).value == "09-411 PŁOCK"
            assert "Uzupełniono z wykazu" in text(form, "#form-error")
            form.action_submit()
            await pilot.pause()
            assert results[0]["account"] == "30 1020 1026 0000 1402 0355 0589"
            assert results[0]["name"] == "ORLEN SPÓŁKA AKCYJNA"

    asyncio.run(scenario())


def test_wizard_lookup_explains_errors_and_keeps_typed_values(seeded, monkeypatch):
    app, _, _ = seeded
    asked = fake_registry(monkeypatch, RegistryError("Nie ma firmy o NIP 7740001454 w wykazie"))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            form = profile_wizard(app.profile_path)
            await app.push_screen(form)
            await pilot.pause()
            form.query_one("#act-nip", Button).press()  # pusty NIP - nie pytamy wykazu
            await pilot.pause()
            assert "najpierw wpisz" in text(form, "#form-error") and not asked
            form.query_one("#f-nip", Input).value = "1234567890"
            form.query_one("#act-nip", Button).press()
            await pilot.pause()
            assert "NIP" in text(form, "#form-error") and not asked  # zła suma kontrolna
            form.query_one("#f-nip", Input).value = "7740001454"
            form.query_one("#f-name", Input).value = "Moja firma"
            form.query_one("#act-nip", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "Nie ma firmy" in text(form, "#form-error")
            assert form.query_one("#f-name", Input).value == "Moja firma"
            assert not form.query_one("#act-nip", Button).disabled

    asyncio.run(scenario())


def test_wizard_warns_about_vat_status_and_many_accounts(seeded, monkeypatch):
    app, _, _ = seeded
    exempt = CompanyData("7740001454", "JAN NOWAK", "UL. KRÓTKA 2", "90-001 ŁÓDŹ", "Zwolniony", ("1" * 26, "2" * 26))
    fake_registry(monkeypatch, exempt)

    async def scenario():
        results = []
        async with app.run_test(size=(120, 40)) as pilot:
            form = profile_wizard(app.profile_path)
            await app.push_screen(form, results.append)
            await pilot.pause()
            form.query_one("#f-nip", Input).value = "7740001454"
            form.query_one("#act-nip", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            note = text(form, "#form-error")
            assert "zwolniona z VAT" in note and "Rachunków w wykazie: 2" in note
            form.action_submit()
            await pilot.pause()
            assert "account" not in results[0]

    asyncio.run(scenario())


def test_editor_fills_buyer_from_nip(seeded, monkeypatch):
    app, _, _ = seeded
    asked = fake_registry(monkeypatch, ORLEN)

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            draft = new_draft(app.profile, date.today(), [], buyer=Party(), net=Decimal(100))
            editor = EditScreen(draft)
            await app.push_screen(editor)
            await pilot.pause()
            lookup = editor.query_one("#buyer_lookup", Button)
            assert lookup.display
            editor.query_one("#buyer_id", Input).value = "PL 774-000-14-54"
            lookup.press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert asked == ["7740001454"]
            values = {wid: editor.query_one(f"#{wid}", Input).value for wid in ("buyer_id", "buyer_name", "buyer_l1")}
            assert values == {"buyer_id": "7740001454", "buyer_name": "ORLEN SPÓŁKA AKCYJNA", "buyer_l1": "CHEMIKÓW 7"}
            assert editor.query_one("#buyer_l2", Input).value == "09-411 PŁOCK"
            assert any("Uzupełniono nazwę i adres" in n.message for n in app._notifications)
            editor.query_one("#buyer_id_type", Select).value = "vat_ue"
            await pilot.pause()
            assert not lookup.display  # Biała lista zna tylko polskie NIP-y

    asyncio.run(scenario())


def test_editor_lookup_refuses_invalid_nip_and_reports_registry_errors(seeded, monkeypatch):
    app, _, _ = seeded
    asked = fake_registry(monkeypatch, RegistryError("Wyczerpano dzienny limit zapytań"))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            draft = new_draft(app.profile, date.today(), [], buyer=Party(), net=Decimal(100))
            editor = EditScreen(draft)
            await app.push_screen(editor)
            await pilot.pause()
            editor.query_one("#buyer_id", Input).value = "123"
            editor.query_one("#buyer_lookup", Button).press()
            await pilot.pause()
            assert not asked and app.focused is editor.query_one("#buyer_id")
            editor.query_one("#buyer_id", Input).value = "7740001454"
            editor.query_one("#buyer_name", Input).value = "Wpisane ręcznie"
            editor.query_one("#buyer_lookup", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert any("limit zapytań" in n.message for n in app._notifications)
            assert editor.query_one("#buyer_name", Input).value == "Wpisane ręcznie"

    asyncio.run(scenario())


def test_lookup_button_is_part_of_the_nip_row(seeded):
    app, _, _ = seeded

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            form = profile_wizard(app.profile_path)
            await app.push_screen(form)
            await pilot.pause()
            nip, button = form.query_one("#f-nip"), form.query_one("#act-nip")
            assert nip.region.y == button.region.y and button.region.right <= form.region.right
            assert isinstance(app.screen, FormScreen)

    asyncio.run(scenario())

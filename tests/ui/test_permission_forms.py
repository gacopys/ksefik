"""Formularze uprawnień: pełna walidacja każdej gałęzi, świadomy wybór „wszystkich klientów”, przegląd
przed nadaniem i odbieranie tylko tam, gdzie wolno (UX-23, AT-26)."""

import asyncio

from textual.widgets import Button, Checkbox, Input, Select

from ksefik.client import KsefError
from ksefik.ui.common import ConfirmScreen, FormScreen
from ksefik.ui.permissions import PermissionsScreen

PERSON = {"value": "44051401458", "first": "Anna", "last": "Kowalska", "description": "Księgowa biura"}


async def open_form(app, pilot, kind: str) -> tuple[PermissionsScreen, FormScreen]:
    screen = PermissionsScreen()
    await app.push_screen(screen)
    await pilot.pause()
    screen._grant_type(kind)
    await pilot.pause()
    form = app.screen
    assert isinstance(form, FormScreen)
    return screen, form


def fill(form: FormScreen, values: dict) -> None:
    for key, value in values.items():
        widget = form.query_one(f"#f-{key}")
        widget.value = value


def error(form: FormScreen) -> str:
    return str(form.query_one("#form-error").render())


def test_indirect_grant_needs_a_client_unless_all_clients_is_chosen_deliberately(seeded):
    app, fake, _ = seeded
    fake.grant_indirect_permissions.return_value = "OP"
    fake.wait_for_permission_operation.return_value = {"code": 200}

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            _, form = await open_form(app, pilot, "indirect")
            fill(form, PERSON)
            assert form.query_one("#f-target", Select).value == "Nip"  # domyślnie jeden klient, nie wszyscy
            assert not form.query_one("#f-all_clients", Checkbox).value
            form.action_submit()
            assert app.screen is form and "wskaż klienta" in error(form)
            fill(form, {"target_value": "1234567890"})
            form.action_submit()
            assert "niepoprawny NIP" in error(form)
            fill(form, {"target": "InternalId", "target_value": "5260250274-1"})
            form.action_submit()
            assert "NIP i 5 cyfr" in error(form)
            form.query_one("#f-all_clients", Checkbox).value = True
            await pilot.pause()
            assert not form.query_one("#row-target").display  # wybór klienta znika - zakres jest jawny
            form.action_submit()
            await pilot.pause()
            review = app.screen
            assert isinstance(review, ConfirmScreen)
            assert "wszystkich klientów biura" in str(review.query_one(".dialog-text").render())
            review.query_one("#confirm", Button).press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert fake.grant_indirect_permissions.await_args.args[-1] == ("AllPartners", None)

    asyncio.run(scenario())


def test_review_can_send_the_user_back_to_fix_the_form(seeded):
    app, fake, _ = seeded

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            _, form = await open_form(app, pilot, "entity")
            fill(form, {"nip": "5260250274", "name": "Biuro Bilans Sp. z o.o.", "description": "Obsługa faktur"})
            form.query_one("#f-delegate", Checkbox).value = True
            form.action_submit()
            await pilot.pause()
            review = app.screen
            text = str(review.query_one(".dialog-text").render())
            assert "Biuro Bilans" in text and "Przeglądanie faktur" in text and "może przekazać" in text
            assert review.query_one("#cancel", Button).has_focus and str(review.query_one("#cancel").label) == "Popraw"
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen is form and form.query_one("#f-name", Input).value == "Biuro Bilans Sp. z o.o."
            fake.grant_entity_permissions.assert_not_awaited()

    asyncio.run(scenario())


def test_secondary_identifiers_are_checked_in_every_branch(seeded):
    app, _, _ = seeded

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            _, person = await open_form(app, pilot, "person")
            fill(person, {**PERSON, "kind": "Fingerprint", "value": "A" * 64})
            await pilot.pause()
            fill(person, {"fp_id_type": "Pesel"})
            await pilot.pause()
            fill(person, {"fp_id": "12345678901"})
            person.action_submit()
            assert "PESEL" in error(person) and "suma kontrolna" in error(person)
            person.dismiss(None)
            await pilot.pause()

            _, unit = await open_form(app, pilot, "subunit")
            fill(unit, {"kind": "Pesel", "value": PERSON["value"], "first": "Jan", "last": "Nowak"})
            fill(unit, {"description": "Administrator oddziału", "context_value": "oddział-1"})
            unit.action_submit()
            assert "NIP i 5 cyfr" in error(unit)
            unit.dismiss(None)
            await pilot.pause()

            _, eu = await open_form(app, pilot, "eu_admin")
            fill(eu, {"fingerprint": "B" * 64, "first": "Hans", "last": "Müller", "person_id": "44051401458"})
            fill(eu, {"context": "5438539502-XX", "eu_name": "Beispiel GmbH", "eu_address": "Berlin"})
            fill(eu, {"description": "Administrator UE"})
            eu.action_submit()
            assert "NIP-VAT" in error(eu) or "numer VAT UE" in error(eu)

    asyncio.run(scenario())


def test_revoke_is_off_in_read_only_views_and_after_a_failed_refresh(seeded):
    app, fake, _ = seeded
    item = {"id": "P-1", "permissionScope": "InvoiceRead", "description": "Biuro", "canDelegate": True}
    fake.query_person_permissions.return_value = [item]
    fake.query_personal_permissions.return_value = [item]

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            screen = PermissionsScreen()
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("delete")
            await pilot.pause()
            confirm = app.screen
            assert isinstance(confirm, ConfirmScreen)
            assert "też je stracą" in str(confirm.query_one(".dialog-text").render())  # skutek dla przekazanych
            confirm.dismiss(False)
            await pilot.pause()
            screen.query_one("#view", Select).value = "personal"  # widok tylko do odczytu
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.check_action("revoke", ()) is False
            screen.query_one("#view", Select).value = "granted"
            fake.query_person_permissions.side_effect = KsefError("Brak połączenia z KSeF.")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.selected is None  # nieaktualny wiersz nie jest celem Del
            await pilot.press("delete")
            await pilot.pause()
            assert app.screen is screen

    asyncio.run(scenario())

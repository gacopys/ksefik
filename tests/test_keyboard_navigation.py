import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from textual.widgets import DataTable

from ksefik.ui.invoice_screens import InvoiceScreen
from ksefik.ui.main_screen import MainScreen


@pytest.mark.parametrize("empty_intermediate", [False, True])
@pytest.mark.parametrize("selection_keys", [("down", "up"), ("up", "down")])
def test_enter_opens_selected_invoice_after_returning_and_switching_types(ui, empty_intermediate, selection_keys):
    app, _, store = ui
    app._profile = replace(app.profile, sync=("wystawione", "otrzymane", "podmiot3"))
    for direction in ("issued", "third") if empty_intermediate else ("issued", "received", "third"):
        store.upsert_metadata(
            direction,
            [
                {
                    "ksefNumber": f"{direction}-{index}",
                    "invoiceNumber": f"FV/{index}",
                    "issueDate": date.today().isoformat(),
                    "grossAmount": 123,
                }
                for index in range(3)
            ],
            {},
        )
    store.set_checkpoint("Subject1", datetime.now(UTC))

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            main = MainScreen()
            await app.push_screen(main)
            await pilot.pause()
            table = main.query_one("#invoices", DataTable)
            await pilot.press("down", "up", "enter")
            assert isinstance(app.screen, InvoiceScreen)
            await pilot.press("escape", "right")
            assert app.screen is main and main.direction == "received"
            if empty_intermediate:
                assert table.row_count == 0
                await pilot.press(*selection_keys, "enter")
                assert app.screen is main
                await pilot.press("right")
                assert main.direction == "third"
            await pilot.press(*selection_keys)
            assert table.has_focus
            expected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            await pilot.press("enter")
            assert isinstance(app.screen, InvoiceScreen), f"Enter did not open the invoice; focus is {main.focused!r}"
            assert f"{app.screen.row['direction']}|{app.screen.row['ksef_number']}" == expected
            await pilot.press("escape", "left")
            if empty_intermediate:
                await pilot.press("left")
            await pilot.press("down", "enter")
            assert isinstance(app.screen, InvoiceScreen)
            assert app.screen.row["direction"] == "issued"

    asyncio.run(scenario())

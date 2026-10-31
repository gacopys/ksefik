"""Każdy główny ekran otwiera się w najmniejszym wspieranym oknie (80×24) bez błędów - te same stany co w zrzutach."""

import asyncio
import importlib.util

import pytest

from ui.harness import ROOT, build_app

spec = importlib.util.spec_from_file_location("screenshot", ROOT / "scripts" / "screenshot.py")
screenshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screenshot)


# „maly-terminal” celowo zmniejsza okno poniżej minimum - sprawdza go test_layout.
@pytest.mark.parametrize("name", [n for n in screenshot.SCENARIOS if n != "maly-terminal"])
def test_screen_opens_at_minimum_size(isolated, name):
    app, fake, store = build_app(isolated)

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            default = app.screen
            await screenshot.SCENARIOS[name](app, pilot, fake)
            assert app.screen is not default
            assert app.screen.focused is not None, f"{name}: nic nie ma fokusu"

    try:
        asyncio.run(scenario())
    finally:
        store.close()

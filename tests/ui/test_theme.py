"""Motyw wybrany w aplikacji zostaje po ponownym uruchomieniu."""

import asyncio

from ksefik.profile import load_profile
from ui.harness import build_app


def test_theme_change_is_saved_in_the_profile_and_used_after_restart(seeded, isolated):
    app, _, _ = seeded

    async def scenario(app, expected):
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.theme == expected
            app.theme = "nord"  # jak wybór w palecie poleceń
            await pilot.pause()

    asyncio.run(scenario(app, "textual-dark"))
    assert load_profile(app.profile.path).theme == "nord"
    again, _, store = build_app(isolated, seed=False, profile=load_profile(app.profile.path))
    try:
        asyncio.run(scenario(again, "nord"))
    finally:
        store.close()

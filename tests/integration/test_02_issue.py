"""Wystawianie faktur w aplikacji: szybka faktura, edytor, nabywca z UE, kopia, korekta, UPO, XML, weryfikacja."""

import asyncio
from datetime import date

from helpers import running_app, set_value, wait_notification, wait_screen, wait_until, write_profile
from lxml import etree

from ksefik.fa_parser import parse_invoice
from ksefik.profile import load_profile
from ksefik.ui.editor import EditScreen, LineScreen
from ksefik.ui.invoice_screens import InvoiceScreen, QuickInvoiceScreen, SendScreen, SummaryScreen
from ksefik.ui.main_screen import MainScreen


async def send_summary(pilot) -> str:
    """Przycisk „Wyślij do KSeF” na podsumowaniu → wysyłka → numer KSeF; wraca do listy faktur."""
    summary = await wait_screen(pilot, SummaryScreen)
    assert not summary.blocking, summary.blocking
    await pilot.press("f9", "enter")
    send = await wait_screen(pilot, SendScreen)
    await wait_until(pilot, lambda: send.finished, "zakończenie wysyłki", timeout=300)
    result = str(send.query_one("#result").render())
    assert send.success, result
    number = send.ksef_number
    await pilot.click("#done")
    await wait_screen(pilot, MainScreen)
    return number


async def fetch_from_ksef(company, ksef_number: str) -> bytes:
    async with company.client(token=True) as client:
        return await client.get_invoice(ksef_number)


def xml_text(xml: bytes, element: str) -> list[str]:
    root = etree.fromstring(xml)
    return [e.text or "" for e in root.iter() if etree.QName(e).localname == element]


def test_issue_flows(isolated_app, company, partner, monkeypatch, downloads, opened_urls):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)
    profile = load_profile(path)
    sent: dict[str, str] = {}

    async def scenario():
        async with running_app(path) as (app, pilot):
            main = await wait_screen(pilot, MainScreen)

            # 1. Szybka faktura: domyślny kontrahent i pozycja z profilu, tylko kwota.
            await pilot.press("n")
            quick = await wait_screen(pilot, QuickInvoiceScreen)
            set_value(quick, "#amount", "1 234,56")
            await pilot.press("enter")
            sent["quick"] = await send_summary(pilot)

            # 2. Kontrahent z UE z profilu: EUR, stawka „np II”, odwrotne obciążenie, rachunek walutowy.
            await pilot.press("n")
            quick = await wait_screen(pilot, QuickInvoiceScreen)
            eu_key = next(k for k, p in quick.buyers.items() if p.vat_ue == "DE123456789")
            set_value(quick, "#buyer", eu_key)
            await pilot.pause(0.2)
            set_value(quick, "#amount", "2000")
            await pilot.press("enter")
            summary = await wait_screen(pilot, SummaryScreen)
            assert summary.draft.currency == "EUR" and summary.draft.reverse_charge
            sent["eu"] = await send_summary(pilot)

            # 3. Edytor: kilka pozycji i stawek, zwolnienie, MPP, zapłacono gotówką, uwagi, zamówienie.
            await pilot.press("n")
            await wait_screen(pilot, QuickInvoiceScreen)
            await pilot.press("ctrl+o")
            editor = await wait_screen(pilot, EditScreen)
            await pilot.pause(0.3)
            table = editor.query_one("#lines")
            table.focus()
            await pilot.press("enter")  # edycja pierwszej pozycji
            line = await wait_screen(pilot, LineScreen)
            set_value(line, "#price", "3000")
            set_value(line, "#quantity", "2")
            await pilot.press("ctrl+s")
            await wait_screen(pilot, EditScreen)
            for name, qty, price, rate in (("Książka", "3", "45,50", "5"), ("Szkolenie", "1", "800", "zw")):
                await pilot.press("ctrl+n")
                line = await wait_screen(pilot, LineScreen)
                set_value(line, "#name", name)
                set_value(line, "#quantity", qty)
                set_value(line, "#unit", "szt.")
                set_value(line, "#price", price)
                set_value(line, "#rate", rate)
                await pilot.press("ctrl+s")
                await wait_screen(pilot, EditScreen)
            assert [line.vat_rate for line in editor.lines] == ["23", "5", "zw"]
            set_value(editor, "#exemption", profile.invoice.podstawa_zwolnienia)
            set_value(editor, "#mpp", True)
            set_value(editor, "#pay_form", "1")
            set_value(editor, "#paid", True)
            set_value(editor, "#notes", "Projekt testowy")
            set_value(editor, "#order_number", "ZAM/7")
            set_value(editor, "#place", "Warszawa")
            await pilot.pause(0.2)
            await pilot.press("f5")
            summary = await wait_screen(pilot, SummaryScreen)
            assert str(summary.draft.gross) == "8323.33"  # 6000 + 23% VAT, 136,50 + 5% VAT (6,83), 800 zw
            sent["editor"] = await send_summary(pilot)

            # 4. Podgląd ostatniej faktury: UPO, XML, link weryfikacyjny, „wystaw podobną”.
            await pilot.press("enter")
            preview = await wait_screen(pilot, InvoiceScreen)
            assert preview.row["ksef_number"] == sent["editor"]
            await pilot.press("u")
            await wait_notification(pilot, f"{sent['editor']}_UPO.xml")
            await pilot.press("x")
            await wait_notification(pilot, f"{sent['editor']}.xml")
            await pilot.press("o")
            await pilot.pause(0.2)
            await pilot.press("d")
            editor = await wait_screen(pilot, EditScreen)
            assert len(editor.lines) == 3 and editor.draft.kind == "VAT"
            await pilot.press("f5")
            sent["copy"] = await send_summary(pilot)

            # 5. Korekta pierwszej (szybkiej) faktury: obniżenie ceny.
            main.reload(select=f"issued|{sent['quick']}")
            await pilot.pause(0.3)
            await pilot.press("enter")
            preview = await wait_screen(pilot, InvoiceScreen)
            assert preview.row["ksef_number"] == sent["quick"]
            await pilot.press("k")
            editor = await wait_screen(pilot, EditScreen)
            assert editor.draft.kind == "KOR"
            editor.query_one("#lines").focus()
            await pilot.press("enter")
            line = await wait_screen(pilot, LineScreen)
            set_value(line, "#price", "1000")
            await pilot.press("ctrl+s")
            await wait_screen(pilot, EditScreen)
            set_value(editor, "#corr_reason", "Rabat posprzedażowy")
            set_value(editor, "#corr_effect", "2")
            await pilot.press("f5")
            summary = await wait_screen(pilot, SummaryScreen)
            assert str(summary.draft.gross) == "-288.51"  # (1000 - 1234.56) * 1.23
            sent["correction"] = await send_summary(pilot)
            return app.store

    asyncio.run(scenario())

    # Weryfikacja po stronie KSeF i w plikach.
    quick = asyncio.run(fetch_from_ksef(company, sent["quick"]))
    assert xml_text(quick, "P_15") == ["1518.51"]
    assert xml_text(quick, "NIP")[:2] == [company.nip, partner.nip]

    eu = parse_invoice(asyncio.run(fetch_from_ksef(company, sent["eu"])))
    assert eu.currency == "EUR" and eu.buyer.other_id == "VAT UE: DE123456789"
    assert "Odwrotne obciążenie" in eu.annotations
    assert eu.bank_accounts[0][0] == "PL27114020040000300201355387"

    edited = asyncio.run(fetch_from_ksef(company, sent["editor"]))
    view = parse_invoice(edited)
    assert [line.rate for line in view.lines] == ["23", "5", "zw"]
    assert "Mechanizm podzielonej płatności" in view.annotations
    assert any(a.startswith("Zwolnienie z VAT: art. 43") for a in view.annotations)
    assert view.paid_on is not None and view.payment_form == "Gotówka"
    assert view.notes == [("Uwagi", "Projekt testowy")] and view.orders == ["Zamówienie: ZAM/7"]
    assert view.issue_place == "Warszawa"

    copy = parse_invoice(asyncio.run(fetch_from_ksef(company, sent["copy"])))
    assert [line.name for line in copy.lines] == [line.name for line in view.lines]
    assert copy.number != view.number

    correction = asyncio.run(fetch_from_ksef(company, sent["correction"]))
    assert xml_text(correction, "RodzajFaktury") == ["KOR"]
    assert xml_text(correction, "NrKSeFFaKorygowanej") == [sent["quick"]]
    assert xml_text(correction, "PrzyczynaKorekty") == ["Rabat posprzedażowy"]

    assert opened_urls and opened_urls[0].startswith(f"https://qr-test.ksef.mf.gov.pl/invoice/{company.nip}/")
    saved = sorted(p.name for p in downloads.iterdir())
    assert any(name.endswith("_UPO.xml") for name in saved) and any(
        name.endswith(f"{sent['editor']}.xml") for name in saved
    )
    folder = profile.data_dir / "wystawione"
    assert {p.name.split("_")[-1].removesuffix(".xml") for p in folder.glob("*.xml") if "_UPO" not in p.name} == set(
        sent.values()
    )


JST_CONTRACTOR = """
[[kontrahenci]]
nazwa = "Gmina Testowa"
nip = "{gmina}"
adres_l1 = "ul. Rynek 1"
adres_l2 = "00-950 Testowo"
jst = true

[[kontrahenci.podmioty3]]
nazwa = "Szkoła Podstawowa w Testowie"
nip = "{szkola}"
adres_l1 = "ul. Szkolna 1"
adres_l2 = "00-950 Testowo"
rola = 8
"""


def test_editor_special_cases(isolated_app, company, partner, monkeypatch):
    """Konsument bez NIP, okres sprzedaży, kompensata, EUR z kursem, nabywca spoza UE i JST z odbiorcą."""
    from ksef_env import random_nip

    gmina, szkola = random_nip(), random_nip()
    extra = JST_CONTRACTOR.format(gmina=gmina, szkola=szkola)
    path = write_profile(isolated_app / "profile.toml", company, partner, extra=extra)
    monkeypatch.setenv("KSEF_TOKEN", company.token)
    sent: dict[str, str] = {}

    async def editor_invoice(pilot, fields: dict) -> str:
        await pilot.press("n")
        await wait_screen(pilot, QuickInvoiceScreen)
        await pilot.press("ctrl+o")
        editor = await wait_screen(pilot, EditScreen)
        editor.query_one("#lines").focus()
        await pilot.press("enter")
        line = await wait_screen(pilot, LineScreen)
        set_value(line, "#price", "500")
        await pilot.press("ctrl+s")
        await wait_screen(pilot, EditScreen)
        for selector, value in fields.items():
            set_value(editor, selector, value)
            await pilot.pause(0.1)
        await pilot.press("f5")
        return await send_summary(pilot)

    async def scenario():
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            today = date.today()
            start = today.replace(day=1).strftime("%d.%m.%Y")
            sent["consumer"] = await editor_invoice(
                pilot,
                {
                    "#buyer_id_type": "brak",
                    "#buyer_id": "",
                    "#buyer_name": "Jan Kowalski",
                    "#buyer_l1": "ul. Prywatna 5",
                    "#buyer_l2": "00-500 Warszawa",
                    "#sale_mode": "period",
                    "#period_from": start,
                    "#period_to": today.strftime("%d.%m.%Y"),
                    "#pay_form": "inna",
                    "#pay_other": "kompensata",
                },
            )
            sent["foreign"] = await editor_invoice(
                pilot,
                {
                    "#buyer_id_type": "nr_id",
                    "#buyer_id": "12-3456789",
                    "#buyer_name": "Example Inc.",
                    "#buyer_l1": "1 Main Street",
                    "#buyer_l2": "Springfield",
                    "#buyer_country": "US",
                    "#currency": "EUR",
                    "#exchange_rate": "4,2512",
                    "#exchange_info": "tabela NBP 205/A/NBP/2026",
                    "#account": "PL27114020040000300201355387",
                },
            )
            # JST: podmiot trzeci (odbiorca, rola 8) z profilu kontrahenta trafia na fakturę sam.
            await pilot.press("n")
            quick = await wait_screen(pilot, QuickInvoiceScreen)
            set_value(quick, "#buyer", next(k for k, p in quick.buyers.items() if p.nip == gmina))
            await pilot.pause(0.2)
            set_value(quick, "#amount", "750")
            await pilot.press("enter")
            summary = await wait_screen(pilot, SummaryScreen)
            assert summary.draft.third_parties and not summary.warnings
            sent["jst"] = await send_summary(pilot)

            # Podgląd: kopiowanie numeru KSeF.
            await pilot.press("enter")
            await wait_screen(pilot, InvoiceScreen)
            await pilot.press("c")
            await wait_notification(pilot, "Skopiowano numer KSeF")

    asyncio.run(scenario())

    consumer = asyncio.run(fetch_from_ksef(company, sent["consumer"]))
    assert xml_text(consumer, "BrakID") == ["1"]
    assert xml_text(consumer, "P_6_Od") and not xml_text(consumer, "P_6")
    assert xml_text(consumer, "OpisPlatnosci") == ["kompensata"]

    foreign = asyncio.run(fetch_from_ksef(company, sent["foreign"]))
    assert xml_text(foreign, "NrID") == ["12-3456789"] and xml_text(foreign, "KodKraju")[-1] == "US"
    assert xml_text(foreign, "KodWaluty") == ["EUR"]
    assert xml_text(foreign, "P_14_1W") == ["488.89"]  # 115 EUR * 4,2512
    assert xml_text(foreign, "NrRB") == ["PL27114020040000300201355387"]

    jst = parse_invoice(asyncio.run(fetch_from_ksef(company, sent["jst"])))
    assert jst.third_parties[0].nip == szkola and jst.third_parties[0].role == "JST – odbiorca"
    assert xml_text(asyncio.run(fetch_from_ksef(company, sent["jst"])), "JST") == ["1"]

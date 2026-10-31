<div align="center">

# ksefik

**Krajowy System e-Faktur w terminalu.**<br>
Przeglądaj, wystawiaj i wysyłaj faktury FA(3) do KSeF 2.0 - szybko, z klawiatury, bez przeglądarki.

[![CI](https://github.com/gacopys/ksefik/actions/workflows/ci.yml/badge.svg)](https://github.com/gacopys/ksefik/actions/workflows/ci.yml)
[![Testy z KSeF](https://github.com/gacopys/ksefik/actions/workflows/e2e.yml/badge.svg)](https://github.com/gacopys/ksefik/actions/workflows/e2e.yml)
[![Pokrycie kodu](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/gacopys/ksefik/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/gacopys/ksefik/blob/python-coverage-comment-action-data/htmlcov/index.html)
[![CodeQL](https://github.com/gacopys/ksefik/actions/workflows/codeql.yml/badge.svg)](https://github.com/gacopys/ksefik/actions/workflows/codeql.yml)
<br>
[![PyPI](https://img.shields.io/pypi/v/ksefik?label=PyPI&color=blue)](https://pypi.org/project/ksefik/)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue?logo=python&logoColor=white)](https://www.python.org/)
[![KSeF 2.0](https://img.shields.io/badge/KSeF-2.0%20%C2%B7%20FA(3)-d4213d)](https://ksef.podatki.gov.pl/)
[![Licencja MIT](https://img.shields.io/badge/licencja-MIT-green)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Textual](https://img.shields.io/badge/UI-Textual-8a2be2)](https://textual.textualize.io/)
<br>
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-donate-yellow.svg)](https://buymeacoffee.com/gacopys)
[![PayPal](https://img.shields.io/badge/PayPal-donate-blue.svg)](https://paypal.me/gacopys)

<img src="https://raw.githubusercontent.com/gacopys/ksefik/main/docs/screenshots/lista.png" alt="Lista faktur w ksefik" width="820">

</div>

---

## Dlaczego ksefik?

- ⚡ **Szybko.** Faktury są w lokalnej bazie SQLite z wyszukiwaniem pełnotekstowym - lista i szukanie działają
  natychmiast, także offline. Nowa faktura dla stałego kontrahenta to kilka klawiszy.
- ✅ **Bezpiecznie.** Każda faktura przed wysyłką jest sprawdzana oficjalnym schematem XSD FA(3) i regułami
  biznesowymi (NIP, daty, kurs waluty, MPP, zwolnienia, duplikaty numerów). Widzisz ją dokładnie w takiej
  postaci, w jakiej trafi do KSeF.
- 🧾 **Kompletnie.** Faktury krajowe, UE i eksport, waluty obce, wiele stawek VAT, korekty, MPP, metoda kasowa,
  JST i podmioty trzecie, wysyłka gotowych plików XML (także wsadowa i offline), UPO.
- 🔐 **Całe konto KSeF.** Tokeny, uprawnienia, certyfikaty KSeF, sesje, limity, identyfikatory zbiorcze, Peppol -
  wszystko, co zwykle wymaga Aplikacji Podatnika.
- 🏠 **Twoje dane zostają u Ciebie.** Brak serwera pośredniczącego i telemetrii. Aplikacja łączy się tylko z API KSeF
  Ministerstwa Finansów (i na Twoje żądanie z Białą listą VAT). Token trafia do systemowego pęku kluczy.
- 🏢 **Wiele firm.** Osobny profil, baza i token dla każdej firmy i środowiska (produkcja, demo, test).

<table>
  <tr>
    <td width="50%"><img src="https://raw.githubusercontent.com/gacopys/ksefik/main/docs/screenshots/podsumowanie.png" alt="Podsumowanie faktury przed wysyłką"><br><sub><b>Podsumowanie przed wysyłką</b> - faktura sprawdzona schematem FA(3)</sub></td>
    <td width="50%"><img src="https://raw.githubusercontent.com/gacopys/ksefik/main/docs/screenshots/faktura.png" alt="Podgląd faktury"><br><sub><b>Podgląd faktury</b> - UPO, XML, korekta, wystaw podobną</sub></td>
  </tr>
  <tr>
    <td><img src="https://raw.githubusercontent.com/gacopys/ksefik/main/docs/screenshots/kreator-nip.png" alt="Kreator profilu z pobieraniem danych po NIP"><br><sub><b>Kreator</b> - dane firmy pobrane po NIP z Białej listy VAT</sub></td>
    <td><img src="https://raw.githubusercontent.com/gacopys/ksefik/main/docs/screenshots/konto.png" alt="Konto KSeF"><br><sub><b>Konto KSeF</b> - tokeny, uprawnienia, certyfikaty, limity</sub></td>
  </tr>
</table>

## Instalacja

Potrzebujesz Pythona 3.12+. ksefik jest rozwijany i testowany na Linuksie; macOS powinien działać, Windows
na razie nie jest wspierany. Najprościej przez [uv](https://docs.astral.sh/uv/):

```bash
uv tool install ksefik      # albo: pipx install ksefik
ksefik
```

Bez instalacji: `uvx ksefik`. Wersja rozwojowa prosto z repozytorium:

```bash
git clone https://github.com/gacopys/ksefik.git && cd ksefik
make run                     # to samo co: uv run ksefik
```

## Pierwsze uruchomienie

1. **Kreator** zapyta o dane firmy (NIP, nazwa, adres, rachunek, numeracja) i zapisze je w profilu
   `~/.config/ksefik/profile.toml`. Przycisk „Pobierz dane” obok NIP-u uzupełni nazwę i adres z wykazu podatników
   VAT (Biała lista MF; bez klucza, limit ok. 100 zapytań dziennie).
2. **Token KSeF** wygenerujesz w [Aplikacji Podatnika KSeF](https://ksef.podatki.gov.pl/). Token trafia do
   systemowego pęku kluczy, a gdy go nie ma - do pliku `~/.config/ksefik/token-…` z prawami `0600`.
   Zamiast tokena możesz [logować się certyfikatem](#logowanie-certyfikatem).
3. Gotowe - faktury zsynchronizują się same.

> [!TIP]
> Chcesz najpierw spróbować bez prawdziwych danych? `ksefik --env test` łączy się ze środowiskiem testowym KSeF,
> a `make run-test-env` w repozytorium sam przygotuje fikcyjną firmę i token ([więcej](#piaskownica-testowa)).

## Jak to działa

**Lista faktur.** Zakładki *Wystawione* / *Otrzymane* (oraz *Jako podmiot 3* / *Jako upoważniony*, jeśli
włączysz je w profilu) i *Wszystkie* (`←` `→` albo `1`…`5`), wybór faktury `↑` `↓` (także podczas pisania
w wyszukiwarce), wyszukiwanie pełnotekstowe (`/`) po numerze, kontrahencie, NIP-ie, numerze KSeF i treści
faktury (polskie znaki nie są wymagane: „uslugi” znajdzie „Usługi”), filtr okresu i kontrahenta.

**Podgląd faktury** (`Enter`): `c` kopiuje numer KSeF, `x` zapisuje XML do *Pobrane*, `o` otwiera stronę
weryfikacji w KSeF, `u` pobiera UPO, `d` wystawia podobną fakturę, `k` tworzy **korektę**, `g` pobiera
treść faktury, której jeszcze nie ma w bazie.

**Synchronizacja.** Faktury są pobierane z KSeF do lokalnej bazy. Synchronizacja rusza sama przy starcie, jeśli
ostatnia była ponad 15 minut temu, i na żądanie klawiszem `r`. Pobierany jest tylko przyrost od ostatniej
synchronizacji (KSeF ogranicza liczbę eksportów). Dowolną fakturę ściągniesz też po numerze KSeF (`g` na liście).

**Nowa faktura** (`n`):
1. Wybierasz kontrahenta i pozycję (z profilu albo z historii wystawionych faktur) i wpisujesz kwotę.
2. Podsumowanie pokazuje fakturę dokładnie w takiej postaci, w jakiej trafi do KSeF - po sprawdzeniu
   oficjalnym schematem XSD FA(3) i regułami.
3. `e` przenosi do edycji: wiele pozycji z różnymi stawkami (23, 8, 5, 0% kraj/WDT/eksport, zw, np,
   odwrotne obciążenie, marża…), data sprzedaży albo okres, waluta i kurs, nabywca z UE/spoza UE/bez NIP,
   forma płatności, termin albo „zapłacono”, rachunek, adnotacje (MPP, metoda kasowa, samofakturowanie,
   TP), uwagi, numer zamówienia i umowy. Zmiany zapisują się same jako szkic.
4. `Ctrl+S` w podsumowaniu wysyła fakturę. Widać kolejne kroki, a na końcu numer KSeF.
   XML i UPO zapisują się w `~/.local/share/ksefik/<środowisko>/<NIP>/wystawione/`.

**Korekta** (`k` w podglądzie faktury wystawionej): pozycje startują od stanu sprzed korekty - zmieniasz
ilości i ceny, aplikacja wysyła wiersze „przed” i „po” oraz różnice kwot (metoda zgodna z FA(3)).

**Wysyłka plików XML** (`i`): faktury FA(3) z innego programu - plik, katalog albo wzorzec (`~/faktury/*.xml`).
Każdy plik jest najpierw sprawdzany schematem XSD; wysyłka w jednej sesji interaktywnej albo jako paczka
wsadowa (ZIP szyfrowany AES-256, dzielony na części), z opcją trybu offline.

**Konto KSeF** (`k` na liście albo `Ctrl+P`):

| Sekcja | Co można zrobić |
| --- | --- |
| Sesje i UPO | historia sesji interaktywnych i wsadowych, statusy faktur, UPO faktur i UPO zbiorcze sesji |
| Tokeny KSeF | lista, wygenerowanie tokena z wybranymi uprawnieniami (i użycie go w aplikacji), unieważnienie |
| Uprawnienia | nadane i obowiązujące, własne, podmiotowe, otrzymane, role, jednostki podrzędne, podmioty unijne; nadawanie (osobie, firmie, podmiotowe, pośrednie, administrator jednostki/podmiotu UE) i odbieranie |
| Certyfikaty KSeF | limity, lista, wniosek o certyfikat (logowanie albo offline), zapis do pliku, unieważnienie |
| Sesje logowania | aktywne logowania i ich unieważnianie |
| Limity | limity sesji, certyfikatów i zapytań API |
| Identyfikatory zbiorcze | jedna płatność za wiele faktur - lista, faktury, generowanie |
| Dostawcy Peppol | lista dostawców zarejestrowanych w KSeF |
| Dane testowe | tylko środowisko testowe: fikcyjne podmioty, osoby, uprawnienia, limity, blokada kontekstu |

> [!NOTE]
> KSeF odrzuca faktury z datą wystawienia w przyszłości. Podsumowanie zablokuje taką wysyłkę i podpowie,
> by zmienić datę w edycji.

Środowisko KSeF podasz w profilu albo przy starcie: `ksefik --env test` (`prod` | `demo` | `test`).
Paletę poleceń otwiera `Ctrl+P` - są w niej wszystkie funkcje, także zmiana motywu i wylogowanie.

## Profil firmy - `profile.toml`

Wszystko, co dotyczy Twojej firmy i Twoich faktur, jest w profilu, a nie w kodzie:

- **dane sprzedawcy** (z KRS/REGON/BDO do stopki) i **rachunki bankowe** (także walutowe),
- **kontrahenci** - z NIP-em, numerem VAT UE, innym identyfikatorem albo bez identyfikatora (osoba prywatna);
  z własnym terminem płatności, walutą, domyślną pozycją i podmiotami trzecimi (np. odbiorca dla JST),
- **katalog pozycji** - nazwa, jednostka, stawka VAT, cena, GTU, PKWiU, CN, załącznik nr 15,
- **numeracja** według wzoru (`FV/{nr}/{mm}/{rrrr}`, `{nr}/{m}/{rrrr}`, `FV/{rrrr}/{nr:4}`…) - licznik
  miesięczny, roczny albo ciągły, osobna seria dla korekt,
- **daty** nowej faktury (dzisiaj / koniec miesiąca / dzisiaj za poprzedni miesiąc), termin i forma płatności,
  waluta, MPP, metoda kasowa, podstawa zwolnienia, procedura marży, stopka,
- **logowanie** - token albo certyfikat, kontekst (NIP, identyfikator wewnętrzny, NIP-VAT UE, Peppol),
  dozwolone adresy IP,
- **synchronizacja** - od kiedy i które faktury: wystawione, otrzymane, jako podmiot 3, jako upoważniony.

Opis wszystkich pól z przykładami: [`profile.example.toml`](profile.example.toml). Profil edytujesz
w dowolnym edytorze (`make profile`, w aplikacji `Ctrl+P` → „Edytuj profil”). Błędy - np. literówkę
w nazwie pola albo zły NIP - aplikacja opisze wprost i podpowie poprawną nazwę. Nowych kontrahentów
i pozycje możesz też zapisywać do profilu prosto z edycji faktury.

**Kilka firm:** każda ma własny profil - `ksefik --profile ~/firmy/druga.toml` (albo zmienna
`KSEFIK_PROFILE`). Baza faktur i tokeny są osobne dla każdej firmy i środowiska
(`~/.local/share/ksefik/<środowisko>/<NIP>/`).

### Logowanie certyfikatem

Zamiast tokena możesz logować się podpisem XAdES: certyfikatem KSeF, kwalifikowaną pieczęcią firmy albo
podpisem kwalifikowanym w plikach PEM/DER lub `.p12`:

```toml
[logowanie]
metoda = "certyfikat"
certyfikat = "~/.config/ksefik/certyfikaty/laptop.pem"
klucz = "~/.config/ksefik/certyfikaty/laptop.klucz.pem"
```

Obsługiwane są klucze RSA i EC (P-256). Hasło do klucza nie jest zapisywane - aplikacja zapyta o nie
(albo ustaw `KSEF_KEY_PASSWORD`). Certyfikat KSeF możesz wygenerować w samej aplikacji: Konto KSeF →
Certyfikaty KSeF → `n` (wymaga zalogowania podpisem; klucz powstaje lokalnie).

### Zmienne środowiskowe

| Zmienna | Znaczenie |
| --- | --- |
| `KSEFIK_PROFILE` | ścieżka profilu (zamiast `~/.config/ksefik/profile.toml`) |
| `KSEF_TOKEN` | token KSeF - ma pierwszeństwo przed zapisanym |
| `KSEF_KEY_PASSWORD` | hasło do klucza prywatnego przy logowaniu certyfikatem |
| `VISUAL`, `EDITOR` | edytor profilu (domyślnie `nano`) |
| `XDG_CONFIG_HOME`, `XDG_DATA_HOME` | położenie konfiguracji i danych |

## Bezpieczeństwo i prywatność

- ksefik nie ma żadnego serwera ani telemetrii. Łączy się wyłącznie z API KSeF (`api*.ksef.mf.gov.pl`) i - gdy
  naciśniesz „Pobierz dane” - z Białą listą VAT (`wl-api.mf.gov.pl`).
- Token KSeF jest w systemowym pęku kluczy (Secret Service / Keychain). Hasła do kluczy prywatnych nie są zapisywane.
- Faktury, XML i UPO leżą lokalnie w `~/.local/share/ksefik/`.
- Lukę bezpieczeństwa zgłoś prywatnie - zobacz [SECURITY.md](SECURITY.md).

## Rozwój

```bash
make install          # zależności (uv)
make test             # testy jednostkowe i interfejsu, bez sieci (~30 s, równolegle)
make check            # lint (ruff) + testy z kontrolą pokrycia (minimum 90%)
make format           # formatowanie kodu
make                  # wszystkie komendy
```

Około 700 testów działa bez sieci: logika faktur i XML, klient API na udawanym transporcie oraz **testy interfejsu**,
które sterują prawdziwą aplikacją Textual (klawisze, formularze, układ w 80×24). Pokrycie linii i gałęzi
musi wynosić co najmniej 90% - inaczej CI nie przejdzie.

### Testy end-to-end z testowym KSeF

```bash
make run-integration-test                      # cały zestaw, ok. 4 minut
make run-integration-test ARGS="-k test_06"    # wybrane testy (argumenty pytest)
```

Testy sterują prawdziwą aplikacją, a backendem jest publiczne środowisko testowe KSeF. Każde uruchomienie tworzy
nowe, fikcyjne firmy (losowe NIP-y z samopodpisaną pieczęcią - KSeF akceptuje takie certyfikaty tylko na
środowisku testowym). Testy działają w osobnym katalogu tymczasowym z wyłączonym pękiem kluczy - nie dotykają
Twojego profilu, bazy ani tokenów. Każdy wynik jest sprawdzany także po stronie KSeF. W GitHub Actions te testy
działają co noc i na żądanie (odznaka „Testy z KSeF”).

Zakres: kreator i logowanie (token, certyfikat z hasłem, wylogowanie), wystawianie (szybka faktura, edytor
z wieloma stawkami, UE/spoza UE/konsument/JST, waluty z kursem, korekta, kopia, UPO, XML), synchronizacja
(wystawione, otrzymane, podmiot 3) z wyszukiwaniem i filtrami, pobieranie po numerze KSeF, wysyłka plików XML
(interaktywna i wsadowa, offline), sesje i UPO, tokeny, sesje logowania, limity, Peppol, identyfikatory
zbiorcze, wszystkie rodzaje uprawnień, certyfikaty KSeF, dane testowe, edycja profilu i dokończenie wysyłki
po ponownym uruchomieniu.

### Piaskownica testowa

Na środowisko testowe KSeF nie wysyła się prawdziwych danych, dlatego służy do tego osobna piaskownica
(domyślnie `~/.ksef-test`, zmienisz to przez `TEST_HOME=…`):

```bash
make run-test-env     # za pierwszym razem: fikcyjny NIP, token i profil, potem start aplikacji
make test-env-clean   # usunięcie piaskownicy (np. żeby zacząć od nowego NIP-u)
```

### Architektura

| Moduł | Odpowiedzialność |
| --- | --- |
| `client.py` | asynchroniczny klient całego API KSeF 2.0 (`httpx`) |
| `xades.py`, `crypto.py` | podpis XAdES, wnioski CSR, szyfrowanie AES/RSA-OAEP |
| `invoice.py`, `money.py`, `codes.py` | model faktury, kwoty i stawki, budowa XML FA(3) |
| `fa_parser.py` | odczyt faktur FA(2)/FA(3) |
| `profile.py` | profil użytkownika (TOML) z walidacją i podpowiedziami |
| `store.py`, `sync.py` | lokalna baza SQLite z wyszukiwaniem FTS5, przyrostowa synchronizacja |
| `submissions.py`, `drafts.py` | wysyłka z odzyskiwaniem po przerwaniu, szkice |
| `registry.py` | Biała lista VAT (dane firmy po NIP) |
| `ui/` | ekrany [Textual](https://textual.textualize.io/) |

Zrzuty ekranów w tym README generuje `make docs-screens` - z fikcyjnymi danymi i udawanym KSeF.

## Współtworzenie

Zgłoszenia błędów, pomysły i pull requesty są mile widziane! Zacznij od [CONTRIBUTING.md](CONTRIBUTING.md).
Jeśli ksefik Ci się przydaje, zostaw ⭐ - to pomaga innym go znaleźć. Rozwój możesz też wesprzeć przez
[Buy Me a Coffee](https://buymeacoffee.com/gacopys) albo [PayPal](https://paypal.me/gacopys).

## Zastrzeżenia

ksefik jest niezależnym projektem open source i nie jest powiązany z Ministerstwem Finansów ani Krajową
Administracją Skarbową. Oprogramowanie jest dostarczane „tak jak jest”, bez gwarancji (zob. [licencja](LICENSE)).
Za treść i wysyłkę faktur odpowiada podatnik - przed pierwszym użyciem na produkcji wypróbuj aplikację
na środowisku testowym KSeF.

## Licencja

[MIT](LICENSE) © Paweł Borkowski

# Współtworzenie ksefik

Dzięki, że chcesz pomóc! Każdy wkład się liczy - zgłoszenie błędu, pomysł, poprawka w dokumentacji czy kod.

## Zgłaszanie błędów i pomysłów

- Błąd: [nowe zgłoszenie](https://github.com/gacopys/ksefik/issues/new/choose) z szablonu „Błąd”. Podaj wersję
  (`ksefik --version`), środowisko KSeF i kroki, które prowadzą do błędu.
- **Nigdy nie wklejaj prawdziwych danych**: tokenów, kluczy, NIP-ów kontrahentów, treści faktur ani numerów
  KSeF z produkcji. Zrzut ekranu z prawdziwymi danymi zamaż albo odtwórz problem na środowisku testowym.
- Luki bezpieczeństwa zgłaszaj prywatnie - zobacz [SECURITY.md](SECURITY.md).

## Przygotowanie środowiska

Wystarczy [uv](https://docs.astral.sh/uv/) i `make`:

```bash
git clone https://github.com/gacopys/ksefik.git && cd ksefik
make install
make test          # testy bez sieci, ~30 s
make run-test-env  # aplikacja z fikcyjną firmą na testowym KSeF
```

Opcjonalnie [pre-commit](https://pre-commit.com/) uruchomi ruff przed każdym commitem: `uvx pre-commit install`.

## Zasady dla zmian w kodzie

1. **Testy.** Każda zmiana zachowania ma test. Interfejs testujemy przez Textual Pilot (`tests/ui/`) - klawiszami,
   tak jak użytkownik. `make check` wymaga minimum 90% pokrycia linii i gałęzi.
2. **Styl.** `make format` przed commitem, `make lint` musi przechodzić (ruff, linia do 120 znaków).
3. **Język.** Interfejs, komunikaty i dokumentacja są po polsku - zrozumiale dla przedsiębiorcy, nie dla
   programisty. Komunikat błędu mówi, co się stało i co zrobić dalej.
4. **Układ 80×24.** Każdy ekran musi być używalny w terminalu 80×24 (`tests/ui/test_layout.py`).
5. **Zgodność z KSeF.** Generowany XML musi przechodzić schemat FA(3) (`src/ksefik/schemas/`). Zmiany w obsłudze
   API sprawdź testami end-to-end: `make run-integration-test ARGS="-k …"`.
6. **Dane testowe są fikcyjne.** Używaj NIP-ów z poprawną sumą kontrolną, ale nie należących do realnych osób,
   adresów typu „ul. Przykładowa 1” i domen `example.com`.

## Pull request

- Jedna sprawa na PR, krótki opis „co i dlaczego”, zrzut ekranu przy zmianach w interfejsie
  (`python scripts/screenshot.py --out /tmp/zrzuty NAZWA`).
- CI (lint, testy na Pythonie 3.12-3.14, pokrycie, budowa paczki) musi być zielone.
- Wpisz zmianę w sekcji „Niewydane” w [CHANGELOG.md](CHANGELOG.md).

## Wydanie nowej wersji (dla opiekunów)

1. Podnieś wersję: `uv version --bump minor` (albo `patch`), uzupełnij `CHANGELOG.md`.
2. `git commit -am "Wydanie vX.Y.Z" && git tag vX.Y.Z && git push --follow-tags`.
3. Workflow „Wydanie” zbuduje paczkę, opublikuje ją na PyPI i utworzy GitHub Release.

Zgłaszając zmianę, zgadzasz się na jej udostępnienie na [licencji MIT](LICENSE) i przestrzegasz
[zasad postępowania](CODE_OF_CONDUCT.md).

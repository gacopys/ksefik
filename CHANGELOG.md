# Historia zmian

Format według [Keep a Changelog](https://keepachangelog.com/pl/1.1.0/), wersje według
[Semantic Versioning](https://semver.org/lang/pl/).

## [Niewydane]

## [0.2.0] - 2026-10-31

Pierwsze publiczne wydanie.

### Dodane

- Przeglądanie faktur wystawionych, otrzymanych, jako podmiot 3 i jako upoważniony - lokalna baza SQLite
  z wyszukiwaniem pełnotekstowym i przyrostową synchronizacją z KSeF 2.0.
- Wystawianie faktur FA(3): wiele stawek VAT, waluty obce z kursem, nabywcy z UE i spoza UE, osoby prywatne,
  JST i podmioty trzecie, MPP, metoda kasowa, procedura marży, szkice zapisywane automatycznie.
- Korekty faktur (wiersze „przed” i „po”), wystawianie podobnej faktury, pobieranie UPO i XML.
- Walidacja schematem XSD FA(3) i regułami biznesowymi przed wysyłką.
- Wysyłka plików XML z innych programów - interaktywna, wsadowa (ZIP AES-256) i offline.
- Obsługa konta KSeF: tokeny, uprawnienia, certyfikaty KSeF, sesje, sesje logowania, limity,
  identyfikatory zbiorcze, dostawcy Peppol, dane testowe.
- Logowanie tokenem (pęk kluczy systemu) albo certyfikatem XAdES (RSA, EC P-256, PEM/DER/PKCS#12).
- Profil firmy w TOML z kreatorem pierwszego uruchomienia i pobieraniem danych po NIP z Białej listy VAT.
- Wiele firm i środowisk (produkcja, demo, test) z osobnymi bazami i tokenami.

[Niewydane]: https://github.com/gacopys/ksefik/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/gacopys/ksefik/releases/tag/v0.2.0

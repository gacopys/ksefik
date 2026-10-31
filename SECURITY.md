# Bezpieczeństwo

ksefik obsługuje tokeny KSeF, klucze prywatne i dane z faktur, więc bezpieczeństwo traktujemy poważnie.

## Wspierane wersje

Poprawki bezpieczeństwa trafiają do najnowszej wydanej wersji.

## Zgłaszanie podatności

**Nie zakładaj publicznego zgłoszenia (issue).** Użyj prywatnego formularza GitHub:
[Report a vulnerability](https://github.com/gacopys/ksefik/security/advisories/new).

Opisz problem, wpływ i sposób odtworzenia - najlepiej na środowisku testowym KSeF, bez prawdziwych tokenów
ani danych. Potwierdzimy zgłoszenie w ciągu kilku dni i poinformujemy o planie poprawki. Po wydaniu poprawki
chętnie wymienimy Cię w opisie wydania (jeśli chcesz).

## Co jest w zakresie

- wyciek tokenów, haseł lub kluczy (pliki, logi, komunikaty, zrzuty ekranu),
- błędy w podpisie XAdES, szyfrowaniu faktur lub weryfikacji certyfikatów,
- wysłanie do KSeF innej faktury niż pokazana w podsumowaniu,
- wykonanie kodu przez spreparowany plik XML, profil lub odpowiedź serwera.

Błędy samego KSeF zgłaszaj Ministerstwu Finansów.

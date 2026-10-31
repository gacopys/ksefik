"""Kwoty i daty w formacie, do którego przywykł człowiek w Polsce."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
# Cena jednostkowa w FA(3) (TKwotowy2) ma do 8 miejsc po przecinku.
PRICE_STEP = Decimal("0.00000001")
NBSP = "\u00a0"


def round2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def round_price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_STEP, rounding=ROUND_HALF_UP)


def parse_amount(text: str) -> Decimal | None:
    """Akceptuje m.in. '46696', '46 696,00', '46.696,5', '46696.50'. Zwraca None, gdy nie da się odczytać."""
    cleaned = re.sub(r"[\s _]|zł|pln", "", text.strip(), flags=re.IGNORECASE)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        # Ostatni separator to separator dziesiętny, pozostałe to separatory tysięcy.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d+)?", cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def fmt_amount(value: Decimal | float | None, currency: str | None = None) -> str:
    """46696.5 -> '46 696,50' (z twardą spacją jako separatorem tysięcy)."""
    if value is None:
        return ""
    value = round2(Decimal(str(value)))
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    text = f"{sign}{NBSP.join(groups)},{frac}"
    return f"{text} {currency}" if currency else text


def fmt_qty(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.replace(".", ",")


def parse_date(text: str) -> date | None:
    """Akceptuje 'DD.MM.RRRR', 'D.M.RRRR' i 'RRRR-MM-DD'."""
    text = text.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def fmt_date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else ""


def fmt_account(number: str) -> str:
    """'61109010140000071219812874' -> '61 1090 1014 0000 0712 1981 2874'."""
    digits = "".join(number.split())
    if len(digits) == 26 and digits.isdigit():
        return digits[:2] + " " + " ".join(digits[i : i + 4] for i in range(2, 26, 4))
    return number


def nip_valid(nip: str) -> bool:
    if not re.fullmatch(r"\d{10}", nip):
        return False
    weights = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    checksum = sum(int(d) * w for d, w in zip(nip, weights, strict=False)) % 11
    return checksum != 10 and checksum == int(nip[9])

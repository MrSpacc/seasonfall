"""Определение сезона по календарю (северное полушарие)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

SEASONS = ("winter", "spring", "summer", "autumn")

SEASON_LABELS = {
    "winter": "зима",
    "spring": "весна",
    "summer": "лето",
    "autumn": "осень",
}

EFFECT_LABELS = {
    "rain": "дождь",
    "snow": "снег",
    "leaves": "осенние листья",
    "petals": "весенние лепестки",
    "fluff": "тополиный пух",
    "frost": "зимний иней",
    "none": "ничего",
}


def season_for(moment: datetime) -> str:
    """Метеорологический сезон: декабрь–февраль зима и т. д."""
    month = moment.month
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "autumn"


def moscow_now(zone: str = "Europe/Moscow") -> datetime:
    return datetime.now(ZoneInfo(zone))

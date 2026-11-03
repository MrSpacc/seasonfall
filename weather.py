"""Погода в Москве через Open-Meteo (без API-ключа)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import logging
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from config import LocationConfig, WeatherConfig

_LOGGER = logging.getLogger(__name__)

API_URL = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "snow-wallpaper/0.1 (+https://github.com/)"

SNOW_CODES = {71, 73, 75, 77, 85, 86}
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}
THUNDER_CODES = {95, 96, 99}

CODE_LABELS = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "изморозь",
    51: "слабая морось",
    53: "морось",
    55: "сильная морось",
    56: "ледяная морось",
    57: "сильная ледяная морось",
    61: "слабый дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "ледяной дождь",
    67: "сильный ледяной дождь",
    71: "слабый снег",
    73: "снег",
    75: "сильный снег",
    77: "снежная крупа",
    80: "слабый ливень",
    81: "ливень",
    82: "сильный ливень",
    85: "слабый снегопад",
    86: "сильный снегопад",
    95: "гроза",
    96: "гроза с градом",
    99: "сильная гроза с градом",
}


@dataclass(frozen=True)
class Weather:
    kind: str | None = None          # "rain" | "snow" | None
    intensity: float = 0.0           # 0..1
    code: int | None = None
    temperature: float | None = None
    precipitation_mm: float = 0.0
    snowfall_cm: float = 0.0
    cloud_cover: float = 0.0
    wind_speed_kmh: float = 0.0
    wind_direction_deg: float = 0.0
    is_day: bool = True
    observed_at: str | None = None
    fetched_at: float = 0.0
    stale: bool = False

    @property
    def description(self) -> str:
        if self.code is None:
            return "нет данных"
        return CODE_LABELS.get(self.code, f"код {self.code}")

    @property
    def is_thunderstorm(self) -> bool:
        return self.code in THUNDER_CODES

    @property
    def age_seconds(self) -> float:
        if not self.fetched_at:
            return float("inf")
        return max(0.0, time.time() - self.fetched_at)

    def summary(self) -> str:
        parts = []
        if self.temperature is not None:
            parts.append(f"{self.temperature:+.1f} °C")
        parts.append(self.description)
        if self.kind:
            parts.append(f"{self.kind}, интенсивность {self.intensity:.2f}")
        else:
            parts.append("без осадков")
        if self.precipitation_mm:
            parts.append(f"осадки {self.precipitation_mm:.2f} мм")
        if self.snowfall_cm:
            parts.append(f"снег {self.snowfall_cm:.2f} см")
        if self.wind_speed_kmh:
            parts.append(f"ветер {self.wind_speed_kmh:.0f} км/ч")
        if self.stale:
            parts.append("из кэша")
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Weather":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})

    @classmethod
    def unknown(cls) -> "Weather":
        return cls(stale=True)

    @classmethod
    def from_open_meteo(cls, payload: dict, threshold_mm: float = 0.02) -> "Weather":
        current = payload.get("current") or {}
        code = current.get("weather_code")
        temperature = current.get("temperature_2m")
        precipitation = float(current.get("precipitation") or 0.0)
        snowfall = float(current.get("snowfall") or 0.0)
        kind = classify(code, precipitation, snowfall, temperature, threshold_mm)
        return cls(
            kind=kind,
            intensity=intensity_for(kind, precipitation, snowfall, code),
            code=code,
            temperature=temperature,
            precipitation_mm=precipitation,
            snowfall_cm=snowfall,
            cloud_cover=float(current.get("cloud_cover") or 0.0),
            wind_speed_kmh=float(current.get("wind_speed_10m") or 0.0),
            wind_direction_deg=float(current.get("wind_direction_10m") or 0.0),
            is_day=bool(current.get("is_day", 1)),
            observed_at=current.get("time"),
            fetched_at=time.time(),
        )


def classify(
    code: int | None,
    precipitation_mm: float,
    snowfall_cm: float,
    temperature: float | None,
    threshold_mm: float = 0.02,
) -> str | None:
    """Возвращает "rain"/"snow"/None по данным Open-Meteo.

    Главный критерий — реально выпавшие осадки, а не weather_code: Open-Meteo
    иногда отдаёт код «снег» при нулевых осадках.
    """
    if snowfall_cm > 0.01:
        return "snow"
    if precipitation_mm <= threshold_mm:
        return None
    if code in SNOW_CODES:
        return "snow"
    if temperature is not None and temperature <= 0.5:
        return "snow"
    if code in RAIN_CODES:
        return "rain"
    return "rain"


def intensity_for(kind: str | None, precipitation_mm: float, snowfall_cm: float,
                  code: int | None = None) -> float:
    if kind is None:
        return 0.0
    if code in (51, 53, 55, 56, 57):
        return 0.3
    if kind == "snow":
        cm = max(snowfall_cm, precipitation_mm / 10.0)
        if cm < 0.2:
            return 0.3
        if cm < 0.7:
            return 0.55
        if cm < 1.5:
            return 0.8
        return 1.0
    if precipitation_mm < 0.3:
        return 0.3
    if precipitation_mm < 1.0:
        return 0.5
    if precipitation_mm < 3.0:
        return 0.75
    return 1.0


class WeatherService:
    """Загружает текущую погоду и хранит последний удачный ответ в кэше."""

    def __init__(self, location: LocationConfig, options: WeatherConfig,
                 cache_path: Path | None = None):
        self.location = location
        self.options = options
        self.cache_path = cache_path

    # --- сеть -----------------------------------------------------------
    def build_url(self) -> str:
        params = {
            "latitude": f"{self.location.latitude:.4f}",
            "longitude": f"{self.location.longitude:.4f}",
            "current": (
                "temperature_2m,precipitation,rain,snowfall,weather_code,"
                "cloud_cover,is_day,wind_speed_10m,wind_direction_10m"
            ),
            "timezone": self.location.timezone,
            "forecast_days": "1",
        }
        return f"{API_URL}?{urllib.parse.urlencode(params)}"

    def fetch(self) -> tuple[Weather, bool]:
        """Возвращает (погода, успех). При ошибке — кэш или unknown."""
        request = urllib.request.Request(self.build_url(), headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.options.timeout_seconds) as response:
                payload = json.load(response)
            weather = Weather.from_open_meteo(payload, self.options.precip_threshold_mm)
            self.save_cache(weather)
            _LOGGER.info("Погода обновлена: %s", weather.summary())
            return weather, True
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
            _LOGGER.warning("Не удалось получить погоду: %s", exc)
            cached = self.load_cache()
            if cached is not None:
                return replace(cached, stale=True), False
            return Weather.unknown(), False

    # --- кэш ------------------------------------------------------------
    def load_cache(self) -> Weather | None:
        if self.cache_path is None or not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            weather = Weather.from_dict(data)
        except (OSError, ValueError, TypeError) as exc:
            _LOGGER.debug("Кэш погоды повреждён: %s", exc)
            return None
        max_age = self.options.cache_minutes * 60
        if max_age and weather.age_seconds > max_age:
            return None
        return weather

    def save_cache(self, weather: Weather) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(weather.to_dict(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            _LOGGER.debug("Не удалось сохранить кэш погоды: %s", exc)

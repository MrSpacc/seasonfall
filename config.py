"""Загрузка и проверка конфигурации."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
import os
import tomllib


class ConfigError(Exception):
    """Некорректный конфигурационный файл."""


@dataclass
class LocationConfig:
    name: str = "Москва"
    latitude: float = 55.7558
    longitude: float = 37.6173
    timezone: str = "Europe/Moscow"


@dataclass
class WeatherConfig:
    update_minutes: int = 10
    precip_threshold_mm: float = 0.02
    timeout_seconds: int = 10
    cache_minutes: int = 180


@dataclass
class EffectsConfig:
    rain: bool = True
    snow: bool = True
    autumn_leaves: bool = True
    spring_petals: bool = True
    summer_fluff: bool = True
    winter_frost: bool = True
    lightning: bool = True
    tint: float = 0.07


@dataclass
class RenderConfig:
    fps: int = 30
    density: float = 1.0
    night_dim: float = 0.85
    seed: int | None = None


@dataclass
class Config:
    location: LocationConfig = field(default_factory=LocationConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    effects: EffectsConfig = field(default_factory=EffectsConfig)
    render: RenderConfig = field(default_factory=RenderConfig)


def project_root() -> Path:
    """Каталог проекта, где лежат main.py и config.toml."""
    return Path(__file__).resolve().parent


def cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))


def default_config_path() -> Path:
    """Конфиг по умолчанию — config.toml в корне проекта."""
    return project_root() / "config.toml"


def _coerce(value, default, where: str):
    if default is None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: ожидалось целое число или null")
        return value
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: ожидалось true или false")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where}: ожидалось целое число")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: ожидалось число")
        return float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{where}: ожидалась строка")
        return value
    return value


def _apply(section, data: dict, prefix: str) -> None:
    if not isinstance(data, dict):
        raise ConfigError(f"[{prefix}]: ожидалась таблица")
    known = {f.name for f in fields(section)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"[{prefix}]: неизвестный параметр «{key}»")
        default = getattr(section, key)
        setattr(section, key, _coerce(value, default, f"[{prefix}] {key}"))


def _validate(cfg: Config) -> None:
    loc = cfg.location
    if not -90 <= loc.latitude <= 90:
        raise ConfigError("latitude должна быть в диапазоне -90..90")
    if not -180 <= loc.longitude <= 180:
        raise ConfigError("longitude должна быть в диапазоне -180..180")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(loc.timezone)
    except Exception as exc:  # noqa: BLE001 - сообщение важнее типа
        raise ConfigError(f"неизвестный часовой пояс: {loc.timezone}") from exc

    if not 1 <= cfg.weather.update_minutes <= 180:
        raise ConfigError("update_minutes должна быть в диапазоне 1..180")
    if cfg.weather.precip_threshold_mm < 0:
        raise ConfigError("precip_threshold_mm не может быть отрицательным")
    if not 1 <= cfg.weather.timeout_seconds <= 60:
        raise ConfigError("timeout_seconds должен быть в диапазоне 1..60")
    if cfg.weather.cache_minutes < 0:
        raise ConfigError("cache_minutes не может быть отрицательным")

    if not 0 <= cfg.effects.tint <= 1:
        raise ConfigError("tint должен быть в диапазоне 0..1")
    if not 1 <= cfg.render.fps <= 120:
        raise ConfigError("fps должен быть в диапазоне 1..120")
    if not 0.05 <= cfg.render.density <= 5:
        raise ConfigError("density должен быть в диапазоне 0.05..5")
    if not 0 <= cfg.render.night_dim <= 1:
        raise ConfigError("night_dim должен быть в диапазоне 0..1")


def load_config(path: Path | None = None) -> Config:
    """Читает TOML-конфиг. Без пути берёт config.toml из корня проекта.

    Если файла нет — возвращает значения по умолчанию.
    """
    cfg = Config()
    if path is None:
        path = default_config_path()
    path = Path(path).expanduser()
    if not path.exists():
        return cfg
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    for key in ("location", "weather", "effects", "render"):
        if key in raw:
            _apply(getattr(cfg, key), raw[key], key)
    for key in raw:
        if key not in {"location", "weather", "effects", "render"}:
            raise ConfigError(f"неизвестная секция «{key}»")
    _validate(cfg)
    return cfg

"""Запуск живой анимации рабочего стола и служебные команды."""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from config import (Config, ConfigError, ConfigWatcher, cache_home,
                    default_config_path, load_config)
from effects import FIELD_CLASSES
from seasons import EFFECT_LABELS, SEASON_LABELS, season_for, moscow_now
from weather import WeatherService

log = logging.getLogger("seasonfall")

LOCK_NAME = "seasonfall.lock"


def cache_dir() -> Path:
    return cache_home() / "seasonfall"


# --- блокировка от повторного запуска ------------------------------------
class InstanceLock:
    """Не даёт запустить вторую копию анимации."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # a+ не обрезает файл, чтобы не потерять PID уже работающей копии.
        self._fh = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def holder(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip() or "?"
        except OSError:
            return "?"


# --- общие помощники -----------------------------------------------------
def make_service(cfg: Config) -> WeatherService:
    return WeatherService(cfg.location, cfg.weather, cache_dir() / "weather.json")


def resolve_season(cfg: Config, override: str | None) -> str:
    if override and override != "auto":
        return override
    return season_for(moscow_now(cfg.location.timezone))


def current_weather(service: WeatherService):
    """Погода для превью/статуса: сеть, при ошибке — кэш."""
    cached = service.load_cache()
    if cached is not None and cached.age_seconds < service.options.update_minutes * 60:
        return cached
    weather, _ = service.fetch()
    return weather


def describe_weather(weather, season: str) -> str:
    lines = [f"Сезон: {SEASON_LABELS.get(season, season)}"]
    if weather is None or weather.code is None:
        lines.append("Погода: нет данных (проверьте сеть)")
        return "\n".join(lines)
    lines.append(f"Температура: {weather.temperature:+.1f} °C" if weather.temperature is not None else "Температура: ?")
    lines.append(f"Состояние: {weather.description} (код {weather.code})")
    lines.append(f"Осадки: {weather.precipitation_mm:.2f} мм, снег: {weather.snowfall_cm:.2f} см")
    lines.append(f"Ветер: {weather.wind_speed_kmh:.0f} км/ч, облачность: {weather.cloud_cover:.0f}%")
    if weather.kind == "rain":
        lines.append("На улице дождь — на экране будет дождь.")
    elif weather.kind == "snow":
        lines.append("На улице снег — на экране будет снег.")
    else:
        lines.append("Осадков нет — будет сезонный эффект.")
    if weather.stale:
        lines.append("(данные из кэша)")
    return "\n".join(lines)


# --- превью --------------------------------------------------------------
def run_preview(cfg: Config, args) -> int:
    import preview

    season = resolve_season(cfg, args.season)
    weather = None
    if not args.offline:
        weather = current_weather(make_service(cfg))
    effect = args.effect or "auto"
    try:
        path = preview.make_preview(
            cfg,
            width=args.width,
            height=args.height,
            season=season,
            effect=effect,
            weather=weather,
            path=args.preview,
            seconds=args.seconds,
            fps=max(1, min(60, args.preview_fps)),
        )
    except (OSError, ValueError) as exc:
        log.error("Не удалось создать превью: %s", exc)
        return 1
    print(f"Готово: {path}")
    if weather is not None:
        print(f"Погода: {weather.summary()}")
    print(f"Сезон: {SEASON_LABELS.get(season, season)}; эффект: "
          f"{EFFECT_LABELS.get(effect, effect)}")
    return 0


# --- графический режим ---------------------------------------------------
def run_overlay(cfg: Config, args, config_path: Path) -> int:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GLib, Gtk

    from window import OverlayManager

    season = resolve_season(cfg, args.season)
    service = make_service(cfg)
    weather = service.load_cache()
    if weather is None:
        weather, _ = service.fetch()

    manager = OverlayManager(cfg, weather, season, args.effect)
    slow_effects = {"leaves", "petals", "fluff", "frost", "none"}
    state = {
        "last": time.monotonic(),
        "season_check": 0.0,
        "frame": 0,
        "fps": cfg.render.fps,
        "slow_every": max(1, int(round(cfg.render.fps / 15.0))),
        "source": None,
    }
    stop_event = threading.Event()
    weather_wake = threading.Event()
    weather_lock = threading.Lock()
    weather_state = {"service": service, "minutes": cfg.weather.update_minutes}
    watcher = ConfigWatcher(config_path)

    def post_weather(new_weather) -> bool:
        if new_weather is None:
            return False
        manager.set_weather(new_weather)
        effects = ", ".join(EFFECT_LABELS.get(e, e) for e in manager.effects)
        log.info("Погода обновлена, эффект: %s", effects)
        return False

    def poll_weather() -> None:
        while not stop_event.is_set():
            with weather_lock:
                service = weather_state["service"]
                minutes = weather_state["minutes"]
            try:
                new_weather, ok = service.fetch()
                if ok or new_weather is not None:
                    GLib.idle_add(post_weather, new_weather)
            except Exception as exc:  # noqa: BLE001 - поток не должен падать
                log.warning("Ошибка опроса погоды: %s", exc)
            if stop_event.is_set():
                break
            wait = max(60.0, minutes * 60.0)
            # Новый конфиг будит поток, чтобы погода обновилась сразу.
            if weather_wake.wait(wait):
                weather_wake.clear()

    def tick() -> bool:
        now = time.monotonic()
        dt = min(0.25, max(0.0, now - state["last"]))
        state["last"] = now
        manager.step(dt)
        # Медленные эффекты (листья, пух, иней) не требуют 30 кадров в секунду.
        state["frame"] += 1
        effects = manager.effects
        slow = bool(effects) and all(e in slow_effects for e in effects)
        if not slow or state["frame"] % state["slow_every"] == 0:
            manager.redraw()
        state["season_check"] += dt
        if state["season_check"] >= 60.0:
            state["season_check"] = 0.0
            new_season = resolve_season(cfg, args.season)
            if new_season != manager.season:
                manager.set_season(new_season)
                log.info("Сменился сезон: %s", SEASON_LABELS.get(new_season, new_season))
        return True

    def schedule_ticks() -> None:
        if state["source"] is not None:
            GLib.source_remove(state["source"])
        interval_ms = max(8, int(round(1000 / state["fps"])))
        state["source"] = GLib.timeout_add(interval_ms, tick)

    def reload_config() -> bool:
        """Подхватывает изменения config.toml без перезапуска."""
        if not watcher.changed():
            return True
        try:
            new_cfg = load_config(config_path)
        except (ConfigError, OSError) as exc:
            log.warning("Конфиг не применён, работаю со старым: %s", exc)
            return True
        if args.fps is not None:
            new_cfg.render.fps = args.fps

        old_fps = cfg.render.fps
        effects_changed = new_cfg.effects != cfg.effects
        visuals_changed = (
            new_cfg.render.density != cfg.render.density
            or new_cfg.render.night_dim != cfg.render.night_dim
            or new_cfg.render.seed != cfg.render.seed
        )
        weather_changed = (
            new_cfg.location != cfg.location
            or new_cfg.weather != cfg.weather
        )

        # Меняем поля существующего объекта: окна и сцены держат ссылку на него.
        cfg.location = new_cfg.location
        cfg.weather = new_cfg.weather
        cfg.effects = new_cfg.effects
        cfg.render = new_cfg.render

        manager.apply_config(rebuild=effects_changed or visuals_changed)

        if weather_changed:
            new_service = make_service(cfg)
            with weather_lock:
                weather_state["service"] = new_service
                weather_state["minutes"] = cfg.weather.update_minutes
            weather_wake.set()

        new_season = resolve_season(cfg, args.season)
        if new_season != manager.season:
            manager.set_season(new_season)

        if cfg.render.fps != old_fps:
            state["fps"] = cfg.render.fps
            state["slow_every"] = max(1, int(round(state["fps"] / 15.0)))
            state["last"] = time.monotonic()
            schedule_ticks()

        log.info("Конфигурация перезагружена: %s (эффект: %s)", config_path,
                 ", ".join(EFFECT_LABELS.get(e, e) for e in manager.effects))
        return True

    def quit_handler(*_args) -> None:
        stop_event.set()
        weather_wake.set()
        GLib.idle_add(Gtk.main_quit)

    signal.signal(signal.SIGINT, quit_handler)
    signal.signal(signal.SIGTERM, quit_handler)

    log.info("Эффект: %s; сезон: %s",
             EFFECT_LABELS.get(args.effect or "auto", args.effect or "auto"),
             SEASON_LABELS.get(season, season))
    log.info("Погода: %s", weather.summary() if weather else "нет данных")

    thread = threading.Thread(target=poll_weather, name="weather", daemon=True)
    thread.start()
    schedule_ticks()
    GLib.timeout_add(1000, reload_config)
    try:
        Gtk.main()
    finally:
        stop_event.set()
        weather_wake.set()
        log.info("Остановлено")
    return 0


# --- CLI ----------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seasonfall",
        description="Seasonfall — сезонные живые обои: листья, лепестки, пух, "
                    "иней, а дождь и снег — только когда они реально идут "
                    "в Москве.",
    )
    parser.add_argument("--config", type=Path, default=None,
                        help="путь к config.toml (по умолчанию — в корне проекта)")
    parser.add_argument("--weather", action="store_true",
                        help="показать текущую погоду и выйти")
    parser.add_argument("--preview", metavar="FILE", default=None,
                        help="сохранить кадр в PNG или анимацию в GIF и выйти")
    parser.add_argument("--effect", default=None,
                        choices=["auto", "none", *FIELD_CLASSES.keys()],
                        help="принудительный эффект (по умолчанию auto)")
    parser.add_argument("--season", default=None,
                        choices=["auto", *SEASON_LABELS.keys()],
                        help="принудительный сезон")
    parser.add_argument("--fps", type=int, default=None,
                        help="частота кадров (1..120)")
    parser.add_argument("--preview-fps", type=int, default=20,
                        help="частота кадров GIF-превью")
    parser.add_argument("--width", type=int, default=1280,
                        help="ширина превью")
    parser.add_argument("--height", type=int, default=720,
                        help="высота превью")
    parser.add_argument("--seconds", type=float, default=4.0,
                        help="длительность GIF-превью в секундах")
    parser.add_argument("--offline", action="store_true",
                        help="не обращаться к сети (превью без погоды)")
    parser.add_argument("--debug", action="store_true", help="подробный лог")
    parser.add_argument("--version", action="version", version="Seasonfall 0.1.0")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.fps is not None and not 1 <= args.fps <= 120:
        parser.error("--fps должен быть в диапазоне 1..120")
    if args.width < 2 or args.height < 2:
        parser.error("--width и --height должны быть больше 1")

    config_path = args.config or default_config_path()
    log.debug("Конфиг: %s", config_path)
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        log.error("Ошибка конфигурации: %s", exc)
        return 2
    if args.fps is not None:
        cfg.render.fps = args.fps

    if args.weather:
        season = resolve_season(cfg, args.season)
        weather = current_weather(make_service(cfg))
        print(describe_weather(weather, season))
        return 0

    if args.preview:
        return run_preview(cfg, args)

    lock = InstanceLock(cache_dir() / LOCK_NAME)
    if not lock.acquire():
        log.error("Анимация уже запущена (PID %s).", lock.holder())
        return 3
    try:
        return run_overlay(cfg, args, config_path)
    except Exception:
        log.exception("Не удалось запустить анимацию")
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())

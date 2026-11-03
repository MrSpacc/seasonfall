"""Тесты: сезоны, погода, выбор эффекта, конфиг и превью."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from config import Config, ConfigError, EffectsConfig, default_config_path, load_config
from effects import choose_effect, create_field, effect_intensity, wind_vector
from main import InstanceLock
from preview import make_preview
from scene import Scene
from seasons import EFFECT_LABELS, SEASON_LABELS, season_for
from weather import Weather, WeatherService, classify, intensity_for


class SeasonTests(unittest.TestCase):
    def test_months(self):
        cases = {
            12: "winter", 1: "winter", 2: "winter",
            3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer",
            9: "autumn", 10: "autumn", 11: "autumn",
        }
        for month, expected in cases.items():
            with self.subTest(month=month):
                self.assertEqual(season_for(datetime(2026, month, 15)), expected)

    def test_labels(self):
        for season in SEASON_LABELS:
            self.assertTrue(SEASON_LABELS[season])
        for effect in EFFECT_LABELS:
            self.assertTrue(EFFECT_LABELS[effect])


class ClassifyTests(unittest.TestCase):
    def test_no_precipitation_even_with_snow_code(self):
        # Реальный случай Open-Meteo: код 71, но осадков нет.
        self.assertIsNone(classify(71, 0.0, 0.0, 0.9))

    def test_rain_by_amount(self):
        self.assertEqual(classify(61, 0.8, 0.0, 8.0), "rain")
        self.assertEqual(classify(3, 1.5, 0.0, 12.0), "rain")

    def test_snowfall_wins(self):
        self.assertEqual(classify(61, 0.0, 0.4, 1.0), "snow")

    def test_snow_code_with_precipitation(self):
        self.assertEqual(classify(71, 0.6, 0.0, -3.0), "snow")

    def test_freezing_temperature(self):
        self.assertEqual(classify(61, 0.5, 0.0, -0.2), "snow")

    def test_threshold(self):
        self.assertIsNone(classify(61, 0.01, 0.0, 5.0))
        self.assertEqual(classify(61, 0.03, 0.0, 5.0), "rain")

    def test_intensity(self):
        self.assertEqual(intensity_for(None, 0.0, 0.0), 0.0)
        self.assertLess(intensity_for("rain", 0.1, 0.0), intensity_for("rain", 5.0, 0.0))
        self.assertLessEqual(intensity_for("snow", 10.0, 5.0), 1.0)
        self.assertGreaterEqual(intensity_for("snow", 0.2, 0.1), 0.3)


class EffectChoiceTests(unittest.TestCase):
    def setUp(self):
        self.effects = EffectsConfig()

    def test_rain_beats_season(self):
        weather = Weather(kind="rain", intensity=0.7)
        self.assertEqual(choose_effect(weather, "autumn", self.effects), "rain")

    def test_snow_beats_season(self):
        weather = Weather(kind="snow", intensity=0.7)
        self.assertEqual(choose_effect(weather, "summer", self.effects), "snow")

    def test_seasonal_fallbacks(self):
        self.assertEqual(choose_effect(None, "autumn", self.effects), "leaves")
        self.assertEqual(choose_effect(None, "spring", self.effects), "petals")
        self.assertEqual(choose_effect(None, "summer", self.effects), "fluff")
        self.assertEqual(choose_effect(None, "winter", self.effects), "frost")

    def test_disabled_effect(self):
        effects = EffectsConfig(rain=False)
        weather = Weather(kind="rain", intensity=0.7)
        self.assertEqual(choose_effect(weather, "winter", effects), "frost")

    def test_forced_effect(self):
        self.assertEqual(choose_effect(None, "winter", self.effects, "snow"), "snow")
        self.assertEqual(choose_effect(None, "winter", self.effects, "none"), "none")

    def test_effect_intensity(self):
        self.assertEqual(effect_intensity("leaves", None), 1.0)
        weather = Weather(kind="rain", intensity=0.8)
        self.assertAlmostEqual(effect_intensity("rain", weather), 0.8)

    def test_wind_vector(self):
        self.assertEqual(wind_vector(None), (0.0, 0.0))
        wx, wy = wind_vector(Weather(wind_speed_kmh=10.0, wind_direction_deg=90.0))
        self.assertNotEqual(wx, 0.0)
        self.assertEqual(wy, 0.0)


class SceneTests(unittest.TestCase):
    def test_scene_uses_rain_only_with_weather(self):
        cfg = Config()
        dry = Scene(800, 600, cfg, weather=Weather(code=71), season="autumn")
        self.assertEqual(dry.effect, "leaves")
        wet = Scene(800, 600, cfg, weather=Weather(kind="rain", intensity=0.5),
                    season="autumn")
        self.assertEqual(wet.effect, "rain")

    def test_resize_and_advance(self):
        cfg = Config()
        scene = Scene(640, 360, cfg, season="autumn")
        scene.advance(1.0, fps=30)
        scene.resize(1280, 720)
        self.assertEqual((scene.width, scene.height), (1280, 720))
        self.assertEqual(scene.field.width, 1280)
        self.assertTrue(scene.needs_full_redraw)
        scene.step(1 / 30)
        self.assertTrue(scene.take_dirty() or scene.take_full_redraw())

    def test_create_field_unknown(self):
        self.assertIsNone(create_field("nope", 100, 100))

    def test_all_fields_build(self):
        for effect in ("rain", "snow", "leaves", "petals", "fluff", "frost"):
            with self.subTest(effect=effect):
                field = create_field(effect, 640, 360, intensity=0.8)
                self.assertIsNotNone(field)
                field.step(1 / 30)
                self.assertTrue(field.particles)


class WeatherParsingTests(unittest.TestCase):
    PAYLOAD = {
        "current": {
            "time": "2026-11-03T13:15",
            "temperature_2m": 0.9,
            "precipitation": 0.0,
            "rain": 0.0,
            "snowfall": 0.0,
            "weather_code": 71,
            "cloud_cover": 100,
            "is_day": 1,
            "wind_speed_10m": 4.3,
            "wind_direction_10m": 180,
        }
    }

    def test_parse(self):
        weather = Weather.from_open_meteo(self.PAYLOAD)
        self.assertIsNone(weather.kind)
        self.assertEqual(weather.code, 71)
        self.assertEqual(weather.temperature, 0.9)
        self.assertIn("0.9", weather.summary())
        self.assertTrue(weather.is_day)

    def test_parse_rain(self):
        payload = json.loads(json.dumps(self.PAYLOAD))
        payload["current"].update(precipitation=1.4, weather_code=63, temperature_2m=7.0)
        weather = Weather.from_open_meteo(payload)
        self.assertEqual(weather.kind, "rain")
        self.assertGreater(weather.intensity, 0.0)

    def test_unknown(self):
        weather = Weather.unknown()
        self.assertTrue(weather.stale)
        self.assertEqual(weather.description, "нет данных")

    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weather.json"
            service = WeatherService(Config().location, Config().weather, path)
            weather = Weather.from_open_meteo(self.PAYLOAD)
            service.save_cache(weather)
            loaded = service.load_cache()
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.code, weather.code)

    def test_url(self):
        cfg = Config()
        service = WeatherService(cfg.location, cfg.weather, None)
        url = service.build_url()
        self.assertIn("api.open-meteo.com", url)
        self.assertIn("55.7558", url)
        self.assertIn("precipitation", url)
        self.assertIn("Europe%2FMoscow", url)


class ConfigTests(unittest.TestCase):
    def test_default_config_is_project_root(self):
        expected = Path(__file__).resolve().parent / "config.toml"
        self.assertEqual(default_config_path(), expected)

    def test_missing_file_defaults(self):
        cfg = load_config(Path("/nonexistent/snow/config.toml"))
        self.assertEqual(cfg.location.timezone, "Europe/Moscow")
        self.assertEqual(cfg.render.fps, 30)

    def test_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[render]\nfps = 20\ndensity = 1.5\n\n"
                "[effects]\nrain = false\n\n"
                "[location]\nlatitude = 59.93\nlongitude = 30.31\n",
                encoding="utf-8",
            )
            cfg = load_config(path)
            self.assertEqual(cfg.render.fps, 20)
            self.assertEqual(cfg.render.density, 1.5)
            self.assertFalse(cfg.effects.rain)
            self.assertAlmostEqual(cfg.location.latitude, 59.93)

    def test_unknown_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[render]\nwat = 1\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[render]\nfps = 1000\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_bad_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[render]\nfps = \"много\"\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)


class LockTests(unittest.TestCase):
    def test_second_instance_sees_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snow.lock"
            first = InstanceLock(path)
            self.assertTrue(first.acquire())
            try:
                second = InstanceLock(path)
                self.assertFalse(second.acquire())
                # Второй экземпляр должен видеть PID первого, а не «?».
                self.assertEqual(second.holder(), str(os.getpid()))
            finally:
                first.release()
            # После освобождения блокировку можно взять снова.
            third = InstanceLock(path)
            self.assertTrue(third.acquire())
            third.release()


class PreviewTests(unittest.TestCase):
    def test_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "preview.png"
            result = make_preview(Config(), width=320, height=180, season="autumn",
                                  path=out)
            self.assertTrue(result.exists())
            self.assertGreater(result.stat().st_size, 1000)

    def test_gif(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "preview.gif"
            result = make_preview(Config(), width=160, height=90, season="winter",
                                  path=out, seconds=0.4, fps=5)
            self.assertTrue(result.exists())
            self.assertGreater(result.stat().st_size, 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)

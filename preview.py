"""Отрисовка превью в PNG/GIF без запуска окон (headless)."""

from __future__ import annotations

from pathlib import Path

import cairo
from PIL import Image

from config import Config
from scene import Scene

# Фоновые градиенты подбираются под сезон и эффект.
_SEASON_BG = {
    "winter": ((0.07, 0.10, 0.16), (0.16, 0.22, 0.33)),
    "spring": ((0.10, 0.12, 0.18), (0.25, 0.20, 0.28)),
    "summer": ((0.06, 0.11, 0.16), (0.13, 0.24, 0.29)),
    "autumn": ((0.12, 0.08, 0.07), (0.26, 0.16, 0.11)),
}

_WEATHER_BG = {
    "rain": ((0.05, 0.07, 0.11), (0.12, 0.16, 0.23)),
    "snow": ((0.09, 0.12, 0.18), (0.22, 0.28, 0.38)),
}


def background_for(season: str, effect: str, is_day: bool = True) -> tuple:
    top, bottom = _WEATHER_BG.get(effect) or _SEASON_BG.get(season, _SEASON_BG["autumn"])
    if not is_day:
        top = tuple(c * 0.45 for c in top)
        bottom = tuple(c * 0.5 for c in bottom)
    return top, bottom


def paint_background(cr: cairo.Context, width: int, height: int,
                     colors: tuple) -> None:
    top, bottom = colors
    gradient = cairo.LinearGradient(0, 0, 0, height)
    gradient.add_color_stop_rgb(0.0, *top)
    gradient.add_color_stop_rgb(1.0, *bottom)
    cr.set_source(gradient)
    cr.paint()

    # Мягкое свечение у нижнего края, чтобы сцена не выглядела плоской.
    glow = cairo.RadialGradient(
        width * 0.5, height * 0.95, 0,
        width * 0.5, height * 0.95, max(width, height) * 0.75,
    )
    glow.add_color_stop_rgba(0.0, 0.55, 0.62, 0.75, 0.10)
    glow.add_color_stop_rgba(1.0, 0.55, 0.62, 0.75, 0.0)
    cr.set_source(glow)
    cr.paint()


def render_scene(scene: Scene, width: int, height: int,
                 background: tuple | None = None) -> Image.Image:
    """Рендерит один кадр сцены в PIL-изображение (RGB)."""
    width = max(1, int(width))
    height = max(1, int(height))
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    if background is not None:
        paint_background(cr, width, height, background)
    else:
        cr.set_source_rgb(0.10, 0.11, 0.14)
        cr.paint()
    scene.draw(cr)
    surface.flush()
    data = bytes(surface.get_data())
    image = Image.frombuffer("RGBA", (width, height), data, "raw", "BGRA", 0, 1)
    return image.convert("RGB")


def _background(scene: Scene) -> tuple:
    weather = scene.weather
    is_day = True if weather is None else weather.is_day
    return background_for(scene.season, scene.effect, is_day)


def save_png(scene: Scene, path: Path, width: int, height: int,
             warmup: float = 4.0) -> Path:
    if warmup > 0:
        scene.advance(warmup, fps=30)
    image = render_scene(scene, width, height, _background(scene))
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def save_gif(scene: Scene, path: Path, width: int, height: int,
             seconds: float = 4.0, fps: int = 20, warmup: float = 3.0,
             max_width: int = 720) -> Path:
    """Сохраняет анимированный GIF. Размер уменьшается ради веса файла."""
    if warmup > 0:
        scene.advance(warmup, fps=30)
    scale = min(1.0, max_width / max(1, width))
    out_w = max(2, int(width * scale))
    out_h = max(2, int(height * scale))
    frames = []
    frame_count = max(1, int(seconds * fps))
    background = _background(scene)
    for _ in range(frame_count):
        image = render_scene(scene, width, height, background)
        if (out_w, out_h) != (width, height):
            image = image.resize((out_w, out_h), Image.LANCZOS)
        frames.append(image)
        scene.step(1.0 / fps)
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    duration = max(20, int(round(1000 / fps)))
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=0,
        optimize=True,
        disposal=2,
    )
    return path


def make_preview(config: Config, *, width: int = 1920, height: int = 1080,
                 season: str = "autumn", effect: str | None = "auto",
                 weather=None, path: str | Path = "preview.png",
                 seconds: float = 4.0, fps: int = 20) -> Path:
    """Создаёт PNG или GIF (по расширению) для проверки без Wayland."""
    scene = Scene(width, height, config, weather=weather, season=season,
                  forced_effect=effect)
    out = Path(path)
    if out.suffix.lower() == ".gif":
        return save_gif(scene, out, width, height, seconds=seconds, fps=fps)
    return save_png(scene, out, width, height)


def _demo_weather(effect: str):
    """Синтетическая погода, чтобы посмотреть дождь/снег без сети."""
    from weather import Weather

    if effect == "rain":
        return Weather(kind="rain", intensity=0.6, code=63, temperature=8.0,
                       precipitation_mm=1.2, cloud_cover=90.0, wind_speed_kmh=14.0,
                       wind_direction_deg=240.0, is_day=True)
    if effect == "snow":
        return Weather(kind="snow", intensity=0.7, code=73, temperature=-4.0,
                       precipitation_mm=1.5, snowfall_cm=0.8, cloud_cover=95.0,
                       wind_speed_kmh=9.0, wind_direction_deg=270.0, is_day=True)
    return Weather(code=3, temperature=2.0, cloud_cover=80.0, is_day=True)


if __name__ == "__main__":
    import argparse

    from seasons import season_for, moscow_now

    parser = argparse.ArgumentParser(description="Превью анимации без окон")
    parser.add_argument("output", nargs="?", default="preview.png")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--season", default=None)
    parser.add_argument("--effect", default="auto")
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--fps", type=int, default=20)
    args = parser.parse_args()

    cfg = Config()
    season = args.season or season_for(moscow_now())
    preview_weather = _demo_weather(args.effect)
    print(make_preview(cfg, width=args.width, height=args.height, season=season,
                       effect=args.effect, weather=preview_weather,
                       path=args.output, seconds=args.seconds, fps=args.fps))

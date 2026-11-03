"""Частицы: дождь, снег, листья, лепестки, пух и иней."""

from __future__ import annotations

import math
import random

import cairo

from config import EffectsConfig

EffectName = str

RAIN_COLOR = (0.87, 0.92, 0.98)
SNOW_COLOR = (1.0, 1.0, 1.0)
LEAF_COLORS = [
    (0.78, 0.30, 0.13),
    (0.88, 0.48, 0.14),
    (0.85, 0.63, 0.13),
    (0.64, 0.26, 0.13),
    (0.55, 0.22, 0.14),
    (0.90, 0.71, 0.27),
]
PETAL_COLORS = [
    (0.97, 0.72, 0.80),
    (0.95, 0.60, 0.70),
    (1.00, 0.83, 0.88),
    (0.91, 0.52, 0.64),
]
FLUFF_COLOR = (0.96, 0.95, 0.90)
FROST_COLOR = (0.84, 0.93, 1.0)


def _scale_for(width: int, height: int) -> float:
    return max(0.75, min(2.0, ((width * height) / (1920 * 1080)) ** 0.25))


class BaseField:
    effect: EffectName = "none"

    def __init__(self, width: int, height: int, *, density: float = 1.0,
                 intensity: float = 0.6, wind: tuple[float, float] = (0.0, 0.0),
                 night: bool = False, night_dim: float = 0.85,
                 seed: int | None = None) -> None:
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.density = max(0.05, float(density))
        self.intensity = min(1.0, max(0.0, float(intensity)))
        self.wind = wind
        self.scale = _scale_for(self.width, self.height)
        self.alpha_mul = night_dim if night else 1.0
        self.rng = random.Random(seed)
        self.particles: list = []
        self.build()

    def count(self, per_megapixel: float, minimum: int = 3) -> int:
        mp = self.width * self.height / 1_000_000.0
        return max(minimum, int(per_megapixel * mp * self.density))

    def build(self) -> None:
        raise NotImplementedError

    def resize(self, width: int, height: int, sx: float, sy: float) -> None:
        """Масштабирует существующие частицы под новый размер экрана."""
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.scale = _scale_for(self.width, self.height)
        avg = (sx + sy) * 0.5
        for p in self.particles:
            for name in ("x", "y"):
                if hasattr(p, name):
                    setattr(p, name, getattr(p, name) * (sx if name == "x" else sy))
            for name in ("size", "r", "length", "width", "sway_amp", "vy", "vx"):
                if hasattr(p, name):
                    setattr(p, name, getattr(p, name) * avg)

    def particle_rects(self):
        """Прямоугольники, которые занимают частицы (для частичной перерисовки)."""
        for p in self.particles:
            if not (hasattr(p, "x") and hasattr(p, "y")):
                continue
            if hasattr(p, "length"):
                extent = p.length + p.width + 4.0
            elif hasattr(p, "size"):
                extent = p.size * (3.0 if self.effect == "fluff" else 1.8)
            elif hasattr(p, "r"):
                extent = p.r * (4.0 if self.effect == "frost" else 3.0)
            else:
                extent = 8.0
            margin = extent + 3.0
            yield (p.x - margin, p.y - margin, margin * 2.0, margin * 2.0)

    def step(self, dt: float) -> None:
        raise NotImplementedError

    def draw(self, cr: cairo.Context, alpha: float = 1.0) -> None:
        raise NotImplementedError

    def _wrap(self, p) -> None:
        w, h = self.width, self.height
        margin = 60 * self.scale
        if p.y > h + margin:
            p.y = -margin - self.rng.random() * h * 0.15
            p.x = self.rng.uniform(-margin, w + margin)
        if p.x > w + margin:
            p.x = -margin
        elif p.x < -margin:
            p.x = w + margin


class _Drop:
    __slots__ = ("x", "y", "vy", "length", "width", "alpha")

    def __init__(self, x, y, vy, length, width, alpha):
        self.x = x
        self.y = y
        self.vy = vy
        self.length = length
        self.width = width
        self.alpha = alpha


class RainField(BaseField):
    effect = "rain"

    def build(self) -> None:
        rng = self.rng
        n = self.count(32 + 95 * self.intensity)
        s = self.scale
        for _ in range(n):
            self.particles.append(_Drop(
                x=rng.uniform(-0.15 * self.width, 1.05 * self.width),
                y=rng.uniform(-self.height, self.height),
                vy=rng.uniform(850, 1450) * s * (0.8 + 0.35 * self.intensity),
                length=rng.uniform(11, 27) * s * (0.85 + 0.4 * self.intensity),
                width=rng.uniform(0.55, 1.45) * s,
                alpha=rng.uniform(0.16, 0.46) * (0.75 + 0.5 * self.intensity),
            ))

    def step(self, dt: float) -> None:
        wind_x = self.wind[0] * 1.7
        for p in self.particles:
            p.y += p.vy * dt
            p.x += wind_x * dt
            self._wrap(p)

    def draw(self, cr: cairo.Context, alpha: float = 1.0) -> None:
        a0 = alpha * self.alpha_mul
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        for p in self.particles:
            slope = self.wind[0] * 1.7 / max(1.0, p.vy)
            cr.set_line_width(p.width)
            cr.set_source_rgba(RAIN_COLOR[0], RAIN_COLOR[1], RAIN_COLOR[2], p.alpha * a0)
            cr.move_to(p.x, p.y)
            cr.line_to(p.x - slope * p.length, p.y - p.length)
            cr.stroke()


class _Flake:
    __slots__ = ("x", "y", "r", "vy", "phase", "sway_amp", "sway_speed", "alpha", "sparkle")

    def __init__(self, x, y, r, vy, phase, sway_amp, sway_speed, alpha, sparkle=False):
        self.x = x
        self.y = y
        self.r = r
        self.vy = vy
        self.phase = phase
        self.sway_amp = sway_amp
        self.sway_speed = sway_speed
        self.alpha = alpha
        self.sparkle = sparkle


class SnowField(BaseField):
    effect = "snow"

    def build(self) -> None:
        rng = self.rng
        n = self.count(12 + 55 * self.intensity)
        s = self.scale
        for _ in range(n):
            big = rng.random() < 0.18
            self.particles.append(_Flake(
                x=rng.uniform(0, self.width),
                y=rng.uniform(-self.height, self.height),
                r=rng.uniform(0.8, 2.1) * s * (1.35 if big else 1.0),
                vy=rng.uniform(28, 92) * s * (0.75 + 0.4 * self.intensity),
                phase=rng.uniform(0, math.tau),
                sway_amp=rng.uniform(5, 28) * s,
                sway_speed=rng.uniform(0.5, 1.8),
                alpha=rng.uniform(0.45, 0.92),
                sparkle=big,
            ))

    def step(self, dt: float) -> None:
        wind_x = self.wind[0] * 0.35
        for p in self.particles:
            p.phase += p.sway_speed * dt
            p.x += (math.cos(p.phase) * p.sway_amp + wind_x) * dt
            p.y += p.vy * dt
            self._wrap(p)

    def draw(self, cr: cairo.Context, alpha: float = 1.0) -> None:
        a0 = alpha * self.alpha_mul
        for p in self.particles:
            cr.set_source_rgba(SNOW_COLOR[0], SNOW_COLOR[1], SNOW_COLOR[2], p.alpha * a0)
            cr.arc(p.x, p.y, p.r, 0, math.tau)
            cr.fill()
            if p.sparkle:
                cr.set_line_width(max(0.4, p.r * 0.35))
                cr.move_to(p.x - p.r * 2.2, p.y)
                cr.line_to(p.x + p.r * 2.2, p.y)
                cr.move_to(p.x, p.y - p.r * 2.2)
                cr.line_to(p.x, p.y + p.r * 2.2)
                cr.stroke()


class _Spark:
    __slots__ = ("x", "y", "r", "vy", "phase", "twinkle", "alpha")

    def __init__(self, x, y, r, vy, phase, twinkle, alpha):
        self.x = x
        self.y = y
        self.r = r
        self.vy = vy
        self.phase = phase
        self.twinkle = twinkle
        self.alpha = alpha


class FrostField(BaseField):
    effect = "frost"

    def build(self) -> None:
        rng = self.rng
        n = self.count(14)
        s = self.scale
        for _ in range(n):
            self.particles.append(_Spark(
                x=rng.uniform(0, self.width),
                y=rng.uniform(0, self.height),
                r=rng.uniform(0.6, 1.7) * s,
                vy=rng.uniform(4, 16) * s,
                phase=rng.uniform(0, math.tau),
                twinkle=rng.uniform(0.8, 2.6),
                alpha=rng.uniform(0.25, 0.75),
            ))

    def step(self, dt: float) -> None:
        for p in self.particles:
            p.phase += p.twinkle * dt
            p.y += p.vy * dt
            p.x += math.sin(p.phase * 0.6) * 4 * dt
            self._wrap(p)

    def draw(self, cr: cairo.Context, alpha: float = 1.0) -> None:
        a0 = alpha * self.alpha_mul
        for p in self.particles:
            twinkle = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(p.phase))
            a = p.alpha * a0 * twinkle
            cr.set_source_rgba(FROST_COLOR[0], FROST_COLOR[1], FROST_COLOR[2], a)
            cr.arc(p.x, p.y, p.r, 0, math.tau)
            cr.fill()
            cr.set_line_width(max(0.35, p.r * 0.25))
            cr.set_source_rgba(FROST_COLOR[0], FROST_COLOR[1], FROST_COLOR[2], a * 0.55)
            cr.move_to(p.x - p.r * 3.0, p.y)
            cr.line_to(p.x + p.r * 3.0, p.y)
            cr.move_to(p.x, p.y - p.r * 3.0)
            cr.line_to(p.x, p.y + p.r * 3.0)
            cr.stroke()


class _Leaf:
    __slots__ = ("x", "y", "vx", "vy", "size", "rot", "spin", "phase",
                 "sway_amp", "sway_speed", "alpha", "color", "narrow")

    def __init__(self, x, y, vx, vy, size, rot, spin, phase,
                 sway_amp, sway_speed, alpha, color, narrow):
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.size = size
        self.rot = rot
        self.spin = spin
        self.phase = phase
        self.sway_amp = sway_amp
        self.sway_speed = sway_speed
        self.alpha = alpha
        self.color = color
        self.narrow = narrow


class LeafField(BaseField):
    effect = "leaves"

    def build(self) -> None:
        rng = self.rng
        n = self.count(13)
        s = self.scale
        for _ in range(n):
            self.particles.append(_Leaf(
                x=rng.uniform(0, self.width),
                y=rng.uniform(-self.height, self.height),
                vx=rng.uniform(-22, 22) * s,
                vy=rng.uniform(28, 78) * s,
                size=rng.uniform(5.0, 12.0) * s,
                rot=rng.uniform(0, math.tau),
                spin=rng.uniform(-1.9, 1.9),
                phase=rng.uniform(0, math.tau),
                sway_amp=rng.uniform(12, 46) * s,
                sway_speed=rng.uniform(0.5, 1.4),
                alpha=rng.uniform(0.62, 0.95),
                color=rng.choice(LEAF_COLORS),
                narrow=rng.uniform(0.42, 0.68),
            ))

    def step(self, dt: float) -> None:
        wind_x = self.wind[0] * 0.8
        for p in self.particles:
            p.phase += p.sway_speed * dt
            p.x += (p.vx + math.sin(p.phase) * p.sway_amp + wind_x) * dt
            p.y += p.vy * dt
            p.rot += p.spin * dt
            self._wrap(p)

    def draw(self, cr: cairo.Context, alpha: float = 1.0) -> None:
        a0 = alpha * self.alpha_mul
        for p in self.particles:
            _draw_leaf(cr, p.x, p.y, p.size, p.rot, p.color, p.alpha * a0, p.narrow)


class _Petal:
    __slots__ = ("x", "y", "vx", "vy", "size", "rot", "spin", "phase",
                 "sway_amp", "sway_speed", "alpha", "color")

    def __init__(self, x, y, vx, vy, size, rot, spin, phase,
                 sway_amp, sway_speed, alpha, color):
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.size = size
        self.rot = rot
        self.spin = spin
        self.phase = phase
        self.sway_amp = sway_amp
        self.sway_speed = sway_speed
        self.alpha = alpha
        self.color = color


class PetalField(BaseField):
    effect = "petals"

    def build(self) -> None:
        rng = self.rng
        n = self.count(15)
        s = self.scale
        for _ in range(n):
            self.particles.append(_Petal(
                x=rng.uniform(0, self.width),
                y=rng.uniform(-self.height, self.height),
                vx=rng.uniform(-14, 14) * s,
                vy=rng.uniform(22, 58) * s,
                size=rng.uniform(3.2, 7.2) * s,
                rot=rng.uniform(0, math.tau),
                spin=rng.uniform(-2.6, 2.6),
                phase=rng.uniform(0, math.tau),
                sway_amp=rng.uniform(14, 42) * s,
                sway_speed=rng.uniform(0.7, 1.8),
                alpha=rng.uniform(0.55, 0.9),
                color=rng.choice(PETAL_COLORS),
            ))

    def step(self, dt: float) -> None:
        wind_x = self.wind[0] * 0.9
        for p in self.particles:
            p.phase += p.sway_speed * dt
            p.x += (p.vx + math.sin(p.phase) * p.sway_amp + wind_x) * dt
            p.y += p.vy * dt
            p.rot += p.spin * dt
            self._wrap(p)

    def draw(self, cr: cairo.Context, alpha: float = 1.0) -> None:
        a0 = alpha * self.alpha_mul
        for p in self.particles:
            _draw_petal(cr, p.x, p.y, p.size, p.rot, p.color, p.alpha * a0)


class _Fluff:
    __slots__ = ("x", "y", "vx", "vy", "size", "phase", "sway_amp", "sway_speed", "alpha")

    def __init__(self, x, y, vx, vy, size, phase, sway_amp, sway_speed, alpha):
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.size = size
        self.phase = phase
        self.sway_amp = sway_amp
        self.sway_speed = sway_speed
        self.alpha = alpha


class FluffField(BaseField):
    effect = "fluff"

    def build(self) -> None:
        rng = self.rng
        n = self.count(9)
        s = self.scale
        for _ in range(n):
            self.particles.append(_Fluff(
                x=rng.uniform(0, self.width),
                y=rng.uniform(0, self.height),
                vx=rng.uniform(-10, 10) * s,
                vy=rng.uniform(6, 24) * s,
                size=rng.uniform(1.8, 4.2) * s,
                phase=rng.uniform(0, math.tau),
                sway_amp=rng.uniform(8, 26) * s,
                sway_speed=rng.uniform(0.25, 0.7),
                alpha=rng.uniform(0.25, 0.6),
            ))

    def step(self, dt: float) -> None:
        for p in self.particles:
            p.phase += p.sway_speed * dt
            p.x += (p.vx + math.sin(p.phase) * p.sway_amp + self.wind[0] * 0.5) * dt
            p.y += p.vy * dt
            self._wrap(p)

    def draw(self, cr: cairo.Context, alpha: float = 1.0) -> None:
        a0 = alpha * self.alpha_mul
        for p in self.particles:
            a = p.alpha * a0
            cr.set_source_rgba(FLUFF_COLOR[0], FLUFF_COLOR[1], FLUFF_COLOR[2], a * 0.35)
            cr.arc(p.x, p.y, p.size * 2.6, 0, math.tau)
            cr.fill()
            cr.set_source_rgba(FLUFF_COLOR[0], FLUFF_COLOR[1], FLUFF_COLOR[2], a)
            cr.arc(p.x, p.y, p.size * 0.65, 0, math.tau)
            cr.fill()
            cr.set_line_width(max(0.3, p.size * 0.16))
            cr.set_source_rgba(FLUFF_COLOR[0], FLUFF_COLOR[1], FLUFF_COLOR[2], a * 0.55)
            for i in range(5):
                ang = p.phase * 0.4 + i * math.tau / 5
                cr.move_to(p.x, p.y)
                cr.line_to(p.x + math.cos(ang) * p.size * 2.1,
                           p.y + math.sin(ang) * p.size * 2.1)
            cr.stroke()


def _draw_leaf(cr: cairo.Context, x: float, y: float, size: float, rot: float,
               color: tuple[float, float, float], alpha: float, narrow: float) -> None:
    cr.save()
    cr.translate(x, y)
    cr.rotate(rot)
    cr.scale(size, size)
    cr.move_to(0, -1.0)
    cr.curve_to(narrow, -0.55, narrow, 0.5, 0, 1.0)
    cr.curve_to(-narrow, 0.5, -narrow, -0.55, 0, -1.0)
    cr.close_path()
    cr.set_source_rgba(color[0], color[1], color[2], alpha)
    cr.fill_preserve()
    cr.set_source_rgba(color[0] * 0.55, color[1] * 0.45, color[2] * 0.35, alpha * 0.65)
    cr.set_line_width(0.07)
    cr.stroke()
    cr.move_to(0, -0.75)
    cr.line_to(0, 1.15)
    cr.set_line_width(0.05)
    cr.stroke()
    cr.restore()


def _draw_petal(cr: cairo.Context, x: float, y: float, size: float, rot: float,
                color: tuple[float, float, float], alpha: float) -> None:
    cr.save()
    cr.translate(x, y)
    cr.rotate(rot)
    cr.scale(size, size)
    cr.move_to(0, -1.0)
    cr.curve_to(0.85, -0.45, 0.65, 0.55, 0, 0.9)
    cr.curve_to(-0.65, 0.55, -0.85, -0.45, 0, -1.0)
    cr.close_path()
    cr.set_source_rgba(color[0], color[1], color[2], alpha)
    cr.fill_preserve()
    cr.set_source_rgba(color[0] * 0.7, color[1] * 0.55, color[2] * 0.65, alpha * 0.5)
    cr.set_line_width(0.07)
    cr.stroke()
    cr.restore()


FIELD_CLASSES = {
    "rain": RainField,
    "snow": SnowField,
    "leaves": LeafField,
    "petals": PetalField,
    "fluff": FluffField,
    "frost": FrostField,
}


def choose_effect(weather, season: str, effects: EffectsConfig,
                  forced: str | None = None) -> EffectName:
    """Выбирает эффект: реальные осадки важнее сезонного украшения."""
    if forced and forced != "auto":
        return forced
    if weather is not None:
        if weather.kind == "rain" and effects.rain:
            return "rain"
        if weather.kind == "snow" and effects.snow:
            return "snow"
    if season == "autumn" and effects.autumn_leaves:
        return "leaves"
    if season == "spring" and effects.spring_petals:
        return "petals"
    if season == "summer" and effects.summer_fluff:
        return "fluff"
    if season == "winter" and effects.winter_frost:
        return "frost"
    return "none"


def effect_intensity(effect: str, weather) -> float:
    if weather is not None and weather.kind == effect:
        return max(0.3, weather.intensity)
    if effect == "rain":
        return 0.55
    if effect == "snow":
        return 0.6
    return 1.0


def wind_vector(weather) -> tuple[float, float]:
    """Вектор ветра в пикселях в секунду (направление, куда дует)."""
    if weather is None:
        return (0.0, 0.0)
    speed = min(80.0, max(0.0, weather.wind_speed_kmh or 0.0))
    if speed <= 0.1:
        return (0.0, 0.0)
    angle = math.radians((weather.wind_direction_deg or 0.0) + 180.0)
    return (math.sin(angle) * speed * 3.0, 0.0)


def create_field(effect: str, width: int, height: int, *, density: float = 1.0,
                 intensity: float = 0.6, wind: tuple[float, float] = (0.0, 0.0),
                 night: bool = False, night_dim: float = 0.85,
                 seed: int | None = None) -> BaseField | None:
    cls = FIELD_CLASSES.get(effect)
    if cls is None:
        return None
    return cls(width, height, density=density, intensity=intensity, wind=wind,
               night=night, night_dim=night_dim, seed=seed)

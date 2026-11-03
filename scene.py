"""Сцена: выбор эффекта, плавные переходы и отрисовка."""

from __future__ import annotations

import math
import random
import time

import cairo

from config import Config
from effects import BaseField, choose_effect, create_field, effect_intensity, wind_vector


class Scene:
    """Один экран: хранит частицы и реагирует на погоду."""

    FADE_SECONDS = 1.4
    CELL = 128

    def __init__(self, width: int, height: int, config: Config, *,
                 weather=None, season: str = "autumn",
                 forced_effect: str | None = None, seed: int | None = None) -> None:
        self.config = config
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.weather = weather
        self.season = season
        self.forced_effect = forced_effect
        self.seed = seed
        self._build_number = 0
        self.effect = "none"
        self.field: BaseField | None = None
        self.previous: BaseField | None = None
        self.fade = 0.0
        self._flash = 0.0
        self._next_flash = 0.0
        self._flash_rng = random.Random(seed)
        self._dirty_cells: set[tuple[int, int]] = set()
        self.needs_full_redraw = True
        self._rebuild(crossfade=False)
        self._next_flash = time.monotonic() + self._flash_rng.uniform(4.0, 12.0)

    # --- состояние ------------------------------------------------------
    def set_weather(self, weather) -> bool:
        """Применяет новую погоду. Возвращает True, если эффект изменился."""
        old_effect = self.effect
        self.weather = weather
        self._rebuild(crossfade=True)
        return self.effect != old_effect

    def set_season(self, season: str) -> None:
        if season != self.season:
            self.season = season
            self._rebuild(crossfade=True)

    def resize(self, width: int, height: int) -> None:
        """Подстраивает сцену под новый размер окна без видимой паузы."""
        width = max(1, int(width))
        height = max(1, int(height))
        if width == self.width and height == self.height:
            return
        self.width = width
        self.height = height
        self.needs_full_redraw = True
        self._dirty_cells = set()
        if self.field is not None:
            # Частицы масштабируем пропорционально, чтобы картинка не прыгала.
            old_w = max(1, self.field.width)
            old_h = max(1, self.field.height)
            self.field.resize(width, height, width / old_w, height / old_h)
        self.previous = None
        self.fade = 0.0

    @property
    def is_transitioning(self) -> bool:
        return self.previous is not None and self.fade > 0.0

    def _seed_for_build(self) -> int | None:
        if self.seed is None:
            return None
        self._build_number += 1
        return self.seed + self._build_number * 7919

    def _rebuild(self, crossfade: bool) -> None:
        self.needs_full_redraw = True
        effect = choose_effect(self.weather, self.season,
                               self.config.effects, self.forced_effect)
        intensity = effect_intensity(effect, self.weather)
        wind = wind_vector(self.weather)
        night = bool(self.weather is not None and not self.weather.is_day)
        new_field = create_field(
            effect, self.width, self.height,
            density=self.config.render.density,
            intensity=intensity,
            wind=wind,
            night=night,
            night_dim=self.config.render.night_dim,
            seed=self._seed_for_build(),
        )
        if new_field is None:
            self.effect = effect
            self.field = None
            self.previous = None
            self.fade = 0.0
            return

        changed = new_field.effect != self.effect or self.field is None
        if changed and crossfade and self.field is not None:
            self.previous = self.field
            self.fade = 1.0
        elif changed:
            self.previous = None
            self.fade = 0.0
        else:
            # Тот же эффект, но, возможно, другая интенсивность/ветер.
            self.previous = self.field
            self.fade = 1.0 if crossfade else 0.0
        self.field = new_field
        self.effect = effect
        if not crossfade:
            self.previous = None
            self.fade = 0.0

    def apply_config(self, rebuild: bool = False) -> None:
        """Применяет новые настройки. При rebuild пересоздаёт частицы."""
        self.needs_full_redraw = True
        if rebuild:
            self._rebuild(crossfade=True)

    # --- движение -------------------------------------------------------
    def step(self, dt: float) -> None:
        dt = max(0.0, min(0.1, dt))
        before = self._collect_cells()
        if self.field is not None:
            self.field.step(dt)
        if self.previous is not None:
            self.previous.step(dt)
            if self.fade > 0.0:
                self.fade = max(0.0, self.fade - dt / self.FADE_SECONDS)
                if self.fade == 0.0:
                    self.previous = None
        self._step_lightning(dt)
        self._update_damage(before)

    # --- частичная перерисовка -----------------------------------------
    def _collect_cells(self) -> set[tuple[int, int]]:
        cell = self.CELL
        cells: set[tuple[int, int]] = set()
        for field in (self.field, self.previous):
            if field is None:
                continue
            for x, y, w, h in field.particle_rects():
                c0 = int(x // cell)
                c1 = int((x + w) // cell)
                r0 = int(y // cell)
                r1 = int((y + h) // cell)
                for cy in range(r0, r1 + 1):
                    for cx in range(c0, c1 + 1):
                        cells.add((cx, cy))
        return cells

    def _update_damage(self, before: set[tuple[int, int]]) -> None:
        if self._flash > 0.0:
            # Вспышка молнии закрывает весь экран.
            self.needs_full_redraw = True
            self._dirty_cells = set()
            return
        self._dirty_cells |= before | self._collect_cells()

    def _cells_to_rects(self, cells: set[tuple[int, int]]) -> list[tuple[int, int, int, int]]:
        """Склеивает соседние ячейки в горизонтальные полосы."""
        cell = self.CELL
        rects: list[tuple[int, int, int, int]] = []
        rows: dict[int, list[int]] = {}
        for cx, cy in cells:
            rows.setdefault(cy, []).append(cx)
        for cy, columns in rows.items():
            columns.sort()
            start = prev = columns[0]
            for cx in columns[1:]:
                if cx == prev + 1:
                    prev = cx
                    continue
                rects.append(self._clip_rect(start * cell, cy * cell,
                                             (prev - start + 1) * cell, cell))
                start = prev = cx
            rects.append(self._clip_rect(start * cell, cy * cell,
                                         (prev - start + 1) * cell, cell))
        return [r for r in rects if r[2] > 0 and r[3] > 0]

    def _clip_rect(self, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(self.width, x + w)
        y1 = min(self.height, y + h)
        return (x0, y0, max(0, x1 - x0), max(0, y1 - y0))

    def take_dirty(self) -> list[tuple[int, int, int, int]]:
        dirty = self._cells_to_rects(self._dirty_cells)
        self._dirty_cells = set()
        return dirty

    def take_full_redraw(self) -> bool:
        if self.needs_full_redraw:
            self.needs_full_redraw = False
            return True
        return False

    def _step_lightning(self, dt: float) -> None:
        weather = self.weather
        if (weather is None or not weather.is_thunderstorm
                or not self.config.effects.lightning or self.effect != "rain"):
            self._flash = 0.0
            return
        now = time.monotonic()
        if now >= self._next_flash:
            self._flash = 1.0
            self._next_flash = now + self._flash_rng.uniform(5.0, 14.0)
        if self._flash > 0.0:
            self._flash = max(0.0, self._flash - dt * 5.5)
            if self._flash == 0.0:
                # Стереть последнюю вспышку, которая занимала весь экран.
                self.needs_full_redraw = True

    # --- отрисовка ------------------------------------------------------
    def draw(self, cr: cairo.Context) -> None:
        self._draw_tint(cr)
        if self.previous is not None and self.fade > 0.0:
            self.previous.draw(cr, self.fade)
        if self.field is not None:
            self.field.draw(cr, 1.0 if self.previous is None else 1.0 - self.fade)
        if self._flash > 0.0:
            cr.set_source_rgba(0.92, 0.95, 1.0, 0.28 * self._flash)
            cr.paint()

    def _draw_tint(self, cr: cairo.Context) -> None:
        alpha = self.config.effects.tint
        if alpha <= 0.0 or self.weather is None or self.weather.kind is None:
            return
        if self.weather.kind == "rain":
            cr.set_source_rgba(0.05, 0.09, 0.15, alpha * max(0.35, self.weather.intensity))
        else:
            cr.set_source_rgba(0.72, 0.80, 0.92, alpha * 0.55 * max(0.35, self.weather.intensity))
        cr.paint()

    # --- превью ---------------------------------------------------------
    def advance(self, seconds: float, fps: int = 30) -> None:
        """Прогревает сцену, чтобы частицы были распределены по экрану."""
        steps = max(1, int(seconds * fps))
        for _ in range(steps):
            self.step(1.0 / fps)


def describe(scene: Scene) -> str:
    season = scene.season
    effect = scene.effect
    weather = scene.weather
    parts = [f"сезон: {season}", f"эффект: {effect}"]
    if weather is not None:
        parts.append(weather.summary())
    return "; ".join(parts)

"""Прозрачные окна на слое bottom через gtk-layer-shell."""

from __future__ import annotations

import logging

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")

from gi.repository import Gdk, GLib, Gtk, GtkLayerShell  # noqa: E402

from config import Config  # noqa: E402
from scene import Scene  # noqa: E402

log = logging.getLogger(__name__)

CSS = b"""
window, drawingarea {
    background-color: rgba(0, 0, 0, 0);
}
window {
    box-shadow: none;
    border: none;
}
"""


def install_css(screen: Gdk.Screen) -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(
        screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


class LayerWindow(Gtk.Window):
    """Одно окно на монитор. Клики проходят сквозь него."""

    def __init__(self, monitor: Gdk.Monitor, config: Config, weather,
                 season: str, forced_effect: str | None, seed: int | None) -> None:
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.config = config
        geometry = monitor.get_geometry()
        self.scene = Scene(
            geometry.width or 1920,
            geometry.height or 1080,
            config,
            weather=weather,
            season=season,
            forced_effect=forced_effect,
            seed=seed,
        )
        self.set_decorated(False)
        self.set_app_paintable(True)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_focus_on_map(False)
        self.set_can_focus(False)

        visual = self.get_screen().get_rgba_visual()
        if visual is not None:
            self.set_visual(visual)

        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_namespace(self, "seasonfall")
        GtkLayerShell.set_monitor(self, monitor)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.BOTTOM)
        GtkLayerShell.set_exclusive_zone(self, -1)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM,
                     GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
            GtkLayerShell.set_anchor(self, edge, True)

        self.area = Gtk.DrawingArea()
        self.area.set_can_focus(False)
        self.area.connect("draw", self._on_draw)
        self.area.connect("size-allocate", self._on_size_allocate)
        self.add(self.area)
        self.connect("map-event", self._on_map)
        self.connect("realize", lambda *_: self._reapply_clickthrough())
        self.show_all()
        self._reapply_clickthrough()

    # --- отрисовка ------------------------------------------------------
    def redraw(self) -> None:
        """Перерисовывает только изменившиеся области экрана."""
        if self.scene.take_full_redraw():
            self.area.queue_draw()
            return
        for x, y, w, h in self.scene.take_dirty():
            self.area.queue_draw_area(int(x), int(y), int(w), int(h))

    def _on_draw(self, widget: Gtk.Widget, cr: cairo.Context) -> bool:
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        self.scene.draw(cr)
        return False

    def _on_size_allocate(self, widget: Gtk.Widget, allocation) -> None:
        if allocation.width > 1 and allocation.height > 1:
            self.scene.resize(allocation.width, allocation.height)
        GLib.idle_add(self._reapply_clickthrough)

    # --- прозрачность для ввода ----------------------------------------
    def _on_map(self, *args) -> None:
        GLib.idle_add(self._reapply_clickthrough)

    def _reapply_clickthrough(self) -> bool:
        gdk_window = self.get_window()
        if gdk_window is not None:
            gdk_window.input_shape_combine_region(cairo.Region(), 0, 0)
        return False


class OverlayManager:
    """Создаёт по окну на каждый монитор и раздаёт им погоду."""

    def __init__(self, config: Config, weather, season: str,
                 forced_effect: str | None) -> None:
        self.config = config
        self.weather = weather
        self.season = season
        self.forced_effect = forced_effect
        self.display = Gdk.Display.get_default()
        self.windows: list[LayerWindow] = []
        self._rebuilding = False
        if self.display is not None:
            self.display.connect("monitor-added", self._on_monitors_changed)
            self.display.connect("monitor-removed", self._on_monitors_changed)
        self.rebuild()

    def rebuild(self) -> None:
        for window in self.windows:
            window.destroy()
        self.windows.clear()
        if self.display is None:
            log.error("Не удалось открыть Wayland-дисплей")
            return
        count = self.display.get_n_monitors()
        for index in range(max(1, count)):
            monitor = self.display.get_monitor(index)
            if monitor is None:
                continue
            seed = None
            if self.config.render.seed is not None:
                seed = self.config.render.seed + index * 104729
            window = LayerWindow(monitor, self.config, self.weather,
                                 self.season, self.forced_effect, seed)
            self.windows.append(window)
            geometry = monitor.get_geometry()
            log.info("Монитор %d: %dx%d", index, geometry.width, geometry.height)

    def _on_monitors_changed(self, *args) -> None:
        if not self._rebuilding:
            self._rebuilding = True
            GLib.idle_add(self._rebuild_idle)

    def _rebuild_idle(self) -> bool:
        self._rebuilding = False
        self.rebuild()
        return False

    # --- обновления -----------------------------------------------------
    def set_weather(self, weather) -> None:
        self.weather = weather
        for window in self.windows:
            window.scene.set_weather(weather)

    def set_season(self, season: str) -> None:
        if season == self.season:
            return
        self.season = season
        for window in self.windows:
            window.scene.set_season(season)

    def apply_config(self, rebuild: bool = False) -> None:
        """Раздаёт окнам обновлённый конфиг (объект меняется на месте)."""
        for window in self.windows:
            window.config = self.config
            window.scene.config = self.config
            window.scene.apply_config(rebuild=rebuild)

    def step(self, dt: float) -> None:
        for window in self.windows:
            window.scene.step(dt)

    def redraw(self) -> None:
        for window in self.windows:
            window.redraw()

    @property
    def effects(self) -> list[str]:
        return [window.scene.effect for window in self.windows]

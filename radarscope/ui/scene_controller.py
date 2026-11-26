"""场景 / 电子战联动、海图/NMEA/AIS 定位与模式热切换控制器。

从 MainWindow 拆分而出（与 ConnectionController / ExportController / AlarmBarController 并列），
聚焦以下跨模块联动体验：
    1. 选定演练场景或电子战干扰时，若尚未连接且处于模拟源则自动启动模拟器（真正一点即现）；
    2. `SimStationDialog` 改为非模态实时联动控制台，参数微调即时在 PPI / RD / A 显生效；
    3. 模拟扫描模式（旋转 / 凝视 / 3D 俯仰 RHI）支持菜单/设置内实时热切换；
    4. 统一海图（在线/MBTiles）、NMEA 本船定位、AIS 与陆地静止杂波掩膜状态同步。
"""
from __future__ import annotations

from PyQt5.QtWidgets import QDialog, QMessageBox

from .. import paths
from ..core.maptiles import resolve_provider
from ..core.sim_targets import compute_ew_metrics
from .extra import AisDialog, MapLocationDialog
from .maplayer import MapLayer


class SceneAndMapController:
    """场景 / 电子战 / 海图 / 定位控制器 mixin，运行时依附于 MainWindow 实例。"""

    def _ensure_sim_running(self, reason: str = "") -> bool:
        """若当前未连接且位于模拟源（或用户触发仿真场景），自动连接以消除「点了没画面」断层。"""
        if self.reader is not None:
            return True
        src = self.src_combo.currentData()
        if src != "sim":
            return False
        self._want_connect = True
        self._connect(silent=True)
        if self.reader is not None and reason:
            self.log.push("ok", f"已自动连接模拟源以展示「{reason}」")
        return self.reader is not None

    def _sync_scene_ui(self) -> None:
        sc = self.sim_station.scenario
        jm = self.sim_station.jam_mode
        act_s = getattr(self, "_scene_actions", {}).get(sc)
        if act_s is not None:
            act_s.setChecked(True)
        act_j = getattr(self, "_ew_actions", {}).get(jm)
        if act_j is not None:
            act_j.setChecked(True)
        if hasattr(self, "btn_scene"):
            on = self.sim_station.active()
            self.btn_scene.blockSignals(True)
            self.btn_scene.setChecked(on)
            self.btn_scene.blockSignals(False)
            if jm != "none":
                self.btn_scene.setText(f"干扰:{jm.upper()}")
            elif sc != "none":
                self.btn_scene.setText(f"场景:{sc}")
            elif self.sim_station.objects:
                self.btn_scene.setText(f"目标:{len(self.sim_station.objects)}")
            else:
                self.btn_scene.setText("场景/电子战")
        if hasattr(self, "_update_status_telemetry"):
            self._update_status_telemetry()

    def _set_sim_mode(self, mode: str) -> None:
        """菜单/设置切换模拟器扫描形态（rotating / staring / rhi_3d），运行中实时热重载。"""
        mi = self.mode_combo.findData(mode)
        if mi >= 0 and self.mode_combo.currentIndex() != mi:
            self.mode_combo.setCurrentIndex(mi)
        else:
            self._on_mode_changed()

    def _on_mode_changed(self) -> None:
        mode = str(self.mode_combo.currentData() or "rotating")
        act = getattr(self, "_mode_actions", {}).get(mode)
        if act is not None and not act.isChecked():
            act.blockSignals(True)
            act.setChecked(True)
            act.blockSignals(False)
        self.settings.setValue("sim_mode", mode)
        did = self.drv_combo.currentData()
        if did == "sim":
            was_running = self.reader is not None
            if was_running:
                self._disconnect("")
                self._want_connect = True
                self._connect(silent=True)
                self.log.push("ok", f"已切换模拟工作模式：{self.mode_combo.currentText()}")
            else:
                from ..core.driver import available_drivers

                cls = available_drivers().get("sim")
                if cls is not None:
                    drv = cls()
                    drv.mode = mode
                    self._apply_caps(drv.resolve_capabilities())

    def _apply_scenario(self, key: str, suggested_range: float = 0.0) -> None:
        if suggested_range > 0.0 and abs(float(self.range_spin.value()) - suggested_range) > 1.0:
            self.range_spin.setValue(suggested_range)
        self.sim_station.set_scenario(key, float(self.range_spin.value()))
        self._sync_scene_ui()
        if key == "none":
            self.log.push("info", "已关闭预设演练场景")
        else:
            started = self._ensure_sim_running(key)
            suffix = "" if started else "（请先切换模拟源或点击「连接」）"
            self.log.push("ok", f"已开启场景「{key}」，当前目标 {len(self.sim_station.objects)} 个{suffix}")

    def _set_jam_mode(self, mode: str) -> None:
        self.sim_station.jam_mode = mode
        self.sim_station._jam_t = 0.0
        self._sync_scene_ui()
        if mode != "none":
            self._ensure_sim_running(f"干扰:{mode.upper()}")
            m = compute_ew_metrics(
                mode, self.sim_station.jam_jsr, float(self.range_spin.value()),
                self.sim_station.jam_az_deg, self.sim_station.jam_bw_deg,
                self.sim_station.eccm_agility,
            )
            self.log.push("warn", f"电子战干扰已开启：{mode}（JSR {m['jsr_db']:+.1f} dB · 烧穿 {m['burn_through_m']:.1f} m）")
        else:
            self.log.push("info", "已关闭电子战干扰样式")

    def _on_sim_station_changed(self) -> None:
        self._sync_scene_ui()
        if self.sim_station.active():
            self._ensure_sim_running("模拟电子站")

    def _open_sim_station(self) -> None:
        from .simdialog import SimStationDialog

        dlg = getattr(self, "_sim_dlg", None)
        if dlg is None:
            dlg = SimStationDialog(self.sim_station, float(self.range_spin.value()), self)
            dlg.changed.connect(self._on_sim_station_changed)
            dlg.finished.connect(lambda _r=0: self._sync_scene_ui())
            self._sim_dlg = dlg
        else:
            dlg.range_max = max(2.0, float(self.range_spin.value()))
            dlg._refresh()
            dlg._update_ew_label()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self._sync_scene_ui()

    def _toggle_land_mask(self, on: bool) -> None:
        self.land_mask.enabled = bool(on)
        if not on:
            self.land_mask.reset()
        self.settings.setValue("land_mask", bool(on))
        act = getattr(self, "act_land_mask", None)
        if act is not None and act.isChecked() != bool(on):
            act.blockSignals(True)
            act.setChecked(bool(on))
            act.blockSignals(False)
        if hasattr(self, "_update_status_telemetry"):
            self._update_status_telemetry()
        self.log.push("info", "陆地静止杂波掩膜已开启" if on else "陆地静止杂波掩膜已关闭")

    def _toggle_map(self, on: bool) -> None:
        """联网/离线海图底图：按当前量程自动选瓦片级别，拿本船/中心坐标当原点。"""
        act = getattr(self, "act_map", None)
        if act is not None and act.isChecked() != bool(on):
            act.blockSignals(True)
            act.setChecked(bool(on))
            act.blockSignals(False)
        if on:
            if self.map_layer is None:
                self.map_layer = MapLayer(paths.data_dir() / "tiles", self)
                self.map_layer.ready.connect(self.ppi.update)
            prov = resolve_provider(self._map_provider, self._theme)
            self.map_layer.set_provider(prov)
            if self._mbtiles_path:
                self.map_layer.set_mbtiles(self._mbtiles_path)
            self.map_layer.enabled = True
            self.ppi.map_layer = self.map_layer
            lat, lon = self._ais_ref
            self.map_layer.request(lat, lon, float(self.range_spin.value()),
                                   max(200, self.ppi.width()))
            hint = f"底图已开启（中心 {lat:.5f}, {lon:.5f} · 图源 {self.map_layer.store.attribution}）"
            if float(self.range_spin.value()) < 30.0:
                hint += "；提示：街道/海图需 30m 以上量程才显细节，离线时自动保持透明"
            self.log.push("ok", hint)
        else:
            if self.map_layer is not None:
                self.map_layer.enabled = False
                self.map_layer.stop()
            self.ppi.map_layer = None
            self.ppi2.map_layer = None
            self.log.push("info", "地图底图已关闭")
        self.ppi.update()
        self.ppi2.update()
        if hasattr(self, "_update_status_telemetry"):
            self._update_status_telemetry()

    def _open_map_settings(self) -> None:
        cur_on = bool(self.map_layer is not None and self.map_layer.enabled)
        dlg = MapLocationDialog(
            self._ais_ref[0], self._ais_ref[1], self._map_provider, cur_on,
            mbtiles_path=self._mbtiles_path,
            nmea_port=self._nmea_port,
            nmea_enabled=self._nmea_enabled,
            land_mask=self.land_mask.enabled,
            parent=self,
        )
        if dlg.exec_() != QDialog.Accepted:
            return
        lat, lon, prov, want_on = dlg.values()
        opts = dlg.e6_options()
        self._ais_ref = (lat, lon)
        self._map_provider = prov
        self._mbtiles_path = opts["mbtiles_path"]
        self._nmea_port = int(opts["nmea_port"])
        self._nmea_enabled = bool(opts["nmea_enabled"])
        self._toggle_land_mask(bool(opts["land_mask"]))
        self.settings.setValue("ais_lat", lat)
        self.settings.setValue("ais_lon", lon)
        self.settings.setValue("map_provider", prov)
        self.settings.setValue("mbtiles_path", self._mbtiles_path)
        self.settings.setValue("nmea_port", self._nmea_port)
        self.settings.setValue("nmea_enabled", self._nmea_enabled)

        self.nmea_nav.stop()
        if self._nmea_enabled:
            err = self.nmea_nav.start(self._nmea_port)
            if err:
                self._log_level("warn", err)
            else:
                self.log.push("ok", f"已开启 NMEA 本船定位监听（UDP :{self._nmea_port}）")

        if want_on and dlg.auto_fit_range() and float(self.range_spin.value()) < 100.0:
            self.range_spin.setValue(500.0)
        if self.map_layer is not None:
            self.map_layer.set_mbtiles(self._mbtiles_path or None)
            self.map_layer._key = None
        self._toggle_map(want_on)
        if not want_on:
            self.log.push("ok", f"已保存海图与定位设置：{lat:.5f}, {lon:.5f}")

    def _open_ais(self) -> None:
        dlg = AisDialog(self.ais.port, self._ais_ref[0], self._ais_ref[1],
                        self.ais.running, self)
        if dlg.exec_() != QDialog.Accepted:
            return
        port, lat, lon, on = dlg.values()
        self._ais_ref = (lat, lon)
        self.settings.setValue("ais_port", port)
        self.settings.setValue("ais_lat", lat)
        self.settings.setValue("ais_lon", lon)
        if on:
            err = self.ais.start(port)
            if err:
                QMessageBox.warning(self, "AIS", err)
                return
            self.log.push("ok", f"AIS 已接入，监听 UDP {port}")
        else:
            self.ais.stop()
            self.ppi.set_ais([])
            self.log.push("info", "AIS 已停止")
        if hasattr(self, "_update_status_telemetry"):
            self._update_status_telemetry()

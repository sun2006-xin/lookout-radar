"""场景 / 电子战联动、海图/NMEA/AIS 定位与模式热切换控制器。

从 MainWindow 拆分而出（与 ConnectionController / ExportController / AlarmBarController 并列），
聚焦以下跨模块联动体验：
    1. 选定演练场景或电子战干扰时，若尚未连接且处于模拟源则自动启动模拟器（真正一点即现）；
    2. `SimStationDialog` 改为非模态实时联动控制台，参数微调即时在 PPI / RD / A 显生效；
    3. 模拟扫描模式（旋转 / 凝视 / 3D 俯仰 RHI）支持菜单/设置内实时热切换；
    4. 统一海图（在线/MBTiles）、NMEA 本船定位、AIS 与陆地静止杂波掩膜状态同步。
"""
from __future__ import annotations

from PyQt5.QtWidgets import QDialog, QMessageBox, QWidget

from .. import paths
from ..core.maptiles import resolve_provider
from ..core.sim_targets import compute_ew_metrics, recommended_profile
from .extra import AisDialog, MapLocationDialog
from .maplayer import MapLayer


class SceneAndMapController:
    """场景 / 电子战 / 海图 / 定位控制器 mixin，运行时依附于 MainWindow 实例。"""

    def _sync_dock_chrome(self) -> None:
        """只有常规波形台时隐藏多余的 Dock 标题栏；展开特定场景面板时恢复标签切换。"""
        dock_scope = getattr(self, "dock_scope", None)
        dock_ew = getattr(self, "dock_ew", None)
        dock_zones = getattr(self, "dock_zones", None)
        if dock_scope is None:
            return
        extra_visible = bool(
            (dock_ew is not None and not dock_ew.isHidden())
            or (dock_zones is not None and not dock_zones.isHidden())
        )
        if extra_visible:
            if dock_scope.titleBarWidget() is not None:
                dock_scope.setTitleBarWidget(None)
        else:
            empty = getattr(self, "_empty_dock_title", None)
            if empty is None:
                empty = QWidget(self)
                empty.setFixedHeight(0)
                self._empty_dock_title = empty
            dock_scope.setTitleBarWidget(empty)

    def _update_scene_banner(self, banner_text: str = "") -> None:
        banner = getattr(self, "scene_banner", None)
        lbl = getattr(self, "lbl_scene_banner", None)
        if banner is None or lbl is None:
            return
        sc = self.sim_station.scenario
        jm = self.sim_station.jam_mode
        if not banner_text:
            if jm != "none":
                banner_text = f"电子战对抗演练 ({jm.upper()}) · 已展开电子战控制台 · 可对比 CA / OS / GO-CFAR 与烧穿曲线"
            elif sc != "none":
                prof = recommended_profile(sc) or {}
                banner_text = str(prof.get("banner") or f"当前场景：{sc}")
        show = bool(banner_text) and (sc != "none" or jm != "none")
        lbl.setText(banner_text if show else "")
        banner.setVisible(show)

    def _exit_scenario_to_classic(self) -> None:
        """一键退出专项场景/电子战，还原为日常纯净雷达显控界面。"""
        if self.sim_station.jam_mode != "none":
            self._set_jam_mode("none")
        if self.sim_station.scenario != "none" or self.sim_station.objects:
            self._apply_scenario("none")
        else:
            if hasattr(self, "dock_ew"):
                self.dock_ew.hide()
            if hasattr(self, "dock_zones"):
                self.dock_zones.hide()
            self._sync_dock_chrome()
            self._update_scene_banner("")
            self._sync_scene_ui()

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
                from ..core.i18n import tr
                self.btn_scene.setText(tr("场景"))
        self._update_scene_banner()
        ew_w = getattr(self, "ew_widget", None)
        if ew_w is not None:
            ew_w.sync_from_station(float(self.range_spin.value()))
        sim_dlg = getattr(self, "_sim_dlg", None)
        if sim_dlg is not None and sim_dlg.isVisible():
            sim_dlg.panel.sync_from_station(float(self.range_spin.value()))
        if hasattr(self, "_update_status_telemetry"):
            self._update_status_telemetry()

    def _set_sim_mode(self, mode: str) -> None:
        """菜单/设置切换模拟器扫描形态（rotating / staring / rhi_3d），运行中实时热重载。"""
        if self.src_combo.currentData() == "sim" and self.drv_combo.currentData() != "sim":
            di = self.drv_combo.findData("sim")
            if di >= 0:
                self.drv_combo.setCurrentIndex(di)
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
        prof = recommended_profile(key)
        want_mode = prof.get("sim_mode") if prof else None
        if want_mode and self.src_combo.currentData() == "sim" and (
            self.drv_combo.currentData() != "sim" or str(self.mode_combo.currentData()) != want_mode
        ):
            self._set_sim_mode(str(want_mode))
        elif key == "none" and self.sim_station.jam_mode == "none" and str(self.mode_combo.currentData()) == "rhi_3d":
            self._set_sim_mode("rotating")

        started = self._ensure_sim_running(key) if key != "none" else False

        is_real_dev = (
            self.src_combo.currentData() == "device"
            and self.driver is not None
            and not getattr(self.driver, "simulated", False)
        )
        rng_hint = float(suggested_range or (prof["range_m"] if prof else 0.0))
        if rng_hint > 0.0 and not is_real_dev and abs(float(self.range_spin.value()) - rng_hint) > 1.0:
            self.range_spin.setValue(rng_hint)
        elif rng_hint > 0.0 and is_real_dev and key != "none":
            self.log.push(
                "info",
                f"当前连接真实硬件（量程 {self.range_spin.value():.1f}m），保持硬件物理尺度并在视场内叠加仿真",
            )
        self.sim_station.set_scenario(key, float(self.range_spin.value()))

        # 场景预设 <-> 检测算法 / 杂波抑制 / 跟踪滤波 / 专用面板 自动协同切换
        if prof is not None:
            cfar_val = prof.get("cfar_mode")
            if cfar_val and hasattr(self, "cfar_combo"):
                ci = self.cfar_combo.findData(cfar_val)
                if ci >= 0:
                    self.cfar_combo.blockSignals(True)
                    self.cfar_combo.setCurrentIndex(ci)
                    self.cfar_combo.blockSignals(False)
                    self._cfar_mode = cfar_val
                    self.settings.setValue("cfar_mode", cfar_val)
            want_clut = bool(prof.get("clutter_enabled", False))
            if hasattr(self, "btn_clutter") and self.clutter_map.enabled != want_clut:
                self.btn_clutter.setChecked(want_clut)
            if prof.get("gate_m") or prof.get("filter_mode"):
                self.tracker.configure(
                    gate_m=prof.get("gate_m"),
                    filter_mode=prof.get("filter_mode"),
                    confirm_hits=prof.get("confirm_hits"),
                    confirm_window=prof.get("confirm_window"),
                )
            elif key == "none":
                self.tracker.configure(gate_m=1.5, filter_mode="kalman3d", confirm_hits=1, confirm_window=1)
            zp = getattr(self, "zones_panel", None)
            if zp is not None:
                zp.sync_tracker(
                    self.tracker.filter_mode, self.tracker.assoc_mode,
                    self.tracker.confirm_hits, self.tracker.confirm_window,
                )
            if "show_cov" in prof and hasattr(self, "_set_ppi_flag"):
                self._set_ppi_flag("show_cov", bool(prof["show_cov"]))
            if "show_zones" in prof and prof["show_zones"] is not None and hasattr(self, "_set_ppi_flag"):
                self._set_ppi_flag("show_zones", bool(prof["show_zones"]))
            if "enable_map" in prof and (prof["enable_map"] or key == "none"):
                cur_map = bool(self.map_layer is not None and self.map_layer.enabled)
                if cur_map != bool(prof["enable_map"]):
                    self._toggle_map(bool(prof["enable_map"]))
            if "land_mask" in prof:
                self._toggle_land_mask(bool(prof["land_mask"]))

            # 按场景需要显隐专用停靠面板：常态 "none" 隐藏 dock_ew / dock_zones，还原纯净雷达页面
            want_docks = set(prof.get("docks") or ())
            if self.sim_station.jam_mode != "none":
                want_docks.add("ew")
            if hasattr(self, "dock_ew"):
                if "ew" in want_docks:
                    self.dock_ew.show()
                    self.dock_ew.raise_()
                else:
                    self.dock_ew.hide()
            if hasattr(self, "dock_zones"):
                if "zones" in want_docks:
                    self.dock_zones.show()
                    if "ew" not in want_docks:
                        self.dock_zones.raise_()
                else:
                    self.dock_zones.hide()
            if not want_docks and hasattr(self, "dock_scope"):
                self.dock_scope.show()
                self.dock_scope.raise_()
            self._sync_dock_chrome()

        self._sync_scene_ui()
        if key == "none":
            self.log.push("info", "已退出预设场景，恢复日常雷达监控界面")
        else:
            suffix = "" if started or self.reader is not None else "（请先切换模拟源或点击「连接」）"
            extra = (f" · 联动 CFAR={self._cfar_mode.upper()}, 滤波={self.tracker.filter_mode}, "
                     f"起批={self.tracker.confirm_hits}/{self.tracker.confirm_window}"
                     if prof is not None else "")
            self.log.push("ok", f"已开启场景「{key}」，当前目标 {len(self.sim_station.objects)} 个{extra}{suffix}")

    def _on_canvas_sim_inject(self, x: float, y: float) -> None:
        """在 PPI 画布 Shift + 左键直接点放动态虚拟目标，免除对话框手输坐标。"""
        obj = self.sim_station.inject(x, y, vx=0.0, vy=-0.25, amp=0.88, respawn=False)
        started = self._ensure_sim_running("画布点放目标")
        self._sync_scene_ui()
        suffix = "" if started else "（请连接设备或切换模拟源后查看回波）"
        self.log.push("ok", f"画布直放目标 {obj.name} ({x:.1f}m, {y:.1f}m){suffix}")

    def _on_canvas_sensor_bearing(self, idx: int, new_bearing: float) -> None:
        """画布拖动多站扇区中心线后更新 sensors.json 并刷新覆盖图。"""
        import json

        cfg = self.data_dir / "sensors.json"
        if cfg.exists():
            try:
                raw = json.loads(cfg.read_text(encoding="utf-8"))
                items = raw.get("sensors", raw) if isinstance(raw, dict) else raw
                if isinstance(items, list) and 0 <= idx < len(items):
                    items[idx]["bearing_deg"] = round(float(new_bearing), 1)
                    if isinstance(raw, dict) and "sensors" in raw:
                        raw["sensors"] = items
                    else:
                        raw = items
                    cfg.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:
                self.log.push("warn", f"写入传感器标定角度失败：{exc}")
        drv = getattr(self, "driver", None)
        if drv is not None and hasattr(drv, "_specs") and 0 <= idx < len(drv._specs):
            drv._specs[idx].bearing_deg = round(float(new_bearing), 1)
        self.log.push("ok", f"已标定传感器 S{idx + 1} 朝向 → {new_bearing:.1f}°")

    def _set_jam_mode(self, mode: str) -> None:
        self.sim_station.jam_mode = mode
        self.sim_station._jam_t = 0.0
        if hasattr(self, "dock_ew"):
            if mode != "none":
                self.dock_ew.show()
                self.dock_ew.raise_()
            elif self.sim_station.scenario == "none":
                self.dock_ew.hide()
                if hasattr(self, "dock_scope"):
                    self.dock_scope.show()
                    self.dock_scope.raise_()
            self._sync_dock_chrome()
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
        if (self.sim_station.scenario == "none"
                and self.sim_station.jam_mode == "none"
                and not self.sim_station.objects
                and not self.sim_station.fc_sim):
            if hasattr(self, "dock_ew"):
                self.dock_ew.hide()
            if hasattr(self, "dock_zones"):
                self.dock_zones.hide()
            if hasattr(self, "dock_scope"):
                self.dock_scope.show()
                self.dock_scope.raise_()
            self._sync_dock_chrome()
        elif self.sim_station.jam_mode != "none" and hasattr(self, "dock_ew"):
            self.dock_ew.show()
            self._sync_dock_chrome()
        self._sync_scene_ui()
        if self.sim_station.active():
            self._ensure_sim_running("模拟电子站")

    def _open_sim_station(self) -> None:
        from .simdialog import SimStationDialog

        dlg = getattr(self, "_sim_dlg", None)
        if dlg is None:
            dlg = SimStationDialog(self.sim_station, float(self.range_spin.value()), self)
            dlg.changed.connect(self._on_sim_station_changed)
            dlg.scenarioSelected.connect(lambda sc: self._apply_scenario(sc))
            dlg.exitToNormalRequested.connect(self._exit_scenario_to_classic)
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

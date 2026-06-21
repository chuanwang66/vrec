#!/usr/bin/env python3
"""
vrec menu-bar app — a low-profile recorder/transcriber that lives in the macOS
status bar (like ClashX). No Dock icon, no main window. Everything is driven
from the menu: record now / for a duration / on a schedule, stop, and transcribe
with either the cloud engine (Aliyun) or a local offline model (faster-whisper).

Run:  python3 menubar.py     (normally launched by ./start.sh)
"""

import os
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import rumps

import vrec  # core logic shared with the CLI

IDLE_ICON = "🎙"
REC_ICON = "🔴"
BUSY_ICON = "💬"


def notify(title, subtitle, message=""):
    """Best-effort notification (silently ignored if not permitted)."""
    try:
        rumps.notification(title, subtitle, message)
    except Exception:
        pass


def ask(message, default="", ok="确定"):
    """Show a one-field input dialog; return the text or None if cancelled."""
    resp = rumps.Window(message=message, title="vrec", default_text=str(default),
                        ok=ok, cancel="取消", dimensions=(300, 22)).run()
    return resp.text.strip() if resp.clicked else None


def pick_audio_file():
    """Native open-file dialog; return a path or None."""
    try:
        from AppKit import NSOpenPanel, NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedFileTypes_(["mp3", "m4a", "wav", "aiff", "aac", "flac", "mp4"])
        if panel.runModal() == 1 and panel.URLs():
            return panel.URLs()[0].path()
    except Exception as e:  # noqa: BLE001
        notify("vrec", "无法打开文件选择器", str(e)[:120])
    return None


def pick_image_file():
    """Native open-file dialog for an image; return a path or None."""
    try:
        from AppKit import NSOpenPanel, NSApplication
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedFileTypes_(["png", "jpg", "jpeg", "heic", "gif", "tiff", "bmp"])
        if panel.runModal() == 1 and panel.URLs():
            return panel.URLs()[0].path()
    except Exception as e:  # noqa: BLE001
        notify("vrec", "无法打开文件选择器", str(e)[:120])
    return None


class VrecApp(rumps.App):
    def __init__(self):
        super().__init__(IDLE_ICON, quit_button=None)
        self.proc = None              # ffmpeg subprocess while recording
        self.current_file = None
        self.record_started = None
        self.scheduled = None         # (start_datetime, duration_seconds | None)
        self.transcribing = False
        self.tx_detail = ""           # transcription progress text (e.g. "2/5", "45%")
        self.auto_transcribe = True
        self.has_logo = False

        # --- build menu ---
        self.status_item = rumps.MenuItem("⚪ 待机")   # non-clickable live status line
        self.item_record = rumps.MenuItem("● 开始录音", callback=self.toggle_record)

        timed = rumps.MenuItem("定时录音")
        for label, secs in (("15 分钟", 900), ("30 分钟", 1800),
                            ("1 小时", 3600), ("2 小时", 7200)):
            timed.add(rumps.MenuItem(label, callback=self._timed(secs)))
        timed.add(rumps.separator)
        timed.add(rumps.MenuItem("自定义…", callback=self.timed_custom))

        self.item_schedule = rumps.MenuItem("预约录音…", callback=self.schedule_record)
        self.item_cancel_sched = rumps.MenuItem("取消预约", callback=self.cancel_schedule)

        self.item_tx_last = rumps.MenuItem("转译最近一次录音", callback=self.tx_last)
        self.item_tx_file = rumps.MenuItem("转译音频文件…", callback=self.tx_file)
        self.item_auto = rumps.MenuItem("录完自动转译", callback=self.toggle_auto)
        self.item_auto.state = 1

        # engine submenu
        self.engine_menu = rumps.MenuItem("识别引擎")
        self.item_engine_cloud = rumps.MenuItem("云端 · Aliyun", callback=lambda _: self.set_engine("cloud"))
        self.item_engine_local = rumps.MenuItem("本地 · Whisper（离线）", callback=lambda _: self.set_engine("local"))
        self.engine_menu.add(self.item_engine_cloud)
        self.engine_menu.add(self.item_engine_local)

        # local model submenu
        self.model_menu = rumps.MenuItem("本地模型大小")
        self.model_items = {}
        for m in ("tiny", "base", "small", "medium", "large-v3"):
            it = rumps.MenuItem(m, callback=self._set_model(m))
            self.model_items[m] = it
            self.model_menu.add(it)

        # input-device submenu (mic or loopback / system audio)
        self.mic_menu = rumps.MenuItem("录音设备")

        # settings submenu
        settings = rumps.MenuItem("设置")
        settings.add(rumps.MenuItem("设置 API Key…", callback=self.set_api_key))
        settings.add(rumps.MenuItem("设置 Base URL…", callback=self.set_base_url))
        settings.add(rumps.MenuItem("设置云端模型名…", callback=self.set_model_name))
        settings.add(rumps.separator)
        settings.add(rumps.MenuItem("设置菜单栏图标…", callback=self.set_logo))
        settings.add(rumps.MenuItem("恢复默认图标", callback=self.clear_logo))
        settings.add(rumps.separator)
        settings.add(rumps.MenuItem("打开配置文件", callback=lambda _: self._open(vrec.CONFIG_PATH)))
        settings.add(rumps.MenuItem("查看日志", callback=lambda _: self._open(vrec.LOG_PATH)))

        self.menu = [
            self.status_item,
            None,
            self.item_record,
            timed,
            self.item_schedule,
            self.item_cancel_sched,
            None,
            self.item_tx_last,
            self.item_tx_file,
            self.item_auto,
            None,
            self.engine_menu,
            self.model_menu,
            self.mic_menu,
            None,
            settings,
            rumps.MenuItem("打开录音文件夹", callback=self.open_folder),
            None,
            rumps.MenuItem("退出 vrec", callback=self.quit_app),
        ]

        self._refresh_engine_state()
        self._refresh_model_state()
        self._refresh_mics()
        self.item_cancel_sched.set_callback(None)  # disabled until scheduled
        self._apply_logo_from_config()             # custom menu-bar icon, if set

        self.timer = rumps.Timer(self.tick, 1)
        self.timer.start()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def cfg(self):
        return vrec.load_config()

    def is_recording(self):
        return self.proc is not None and self.proc.poll() is None

    def _open(self, path):
        subprocess.run(["open", str(path)])

    # ------------------------------------------------------------------ #
    # recording
    # ------------------------------------------------------------------ #
    def toggle_record(self, _):
        if self.is_recording():
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self, duration=None):
        if self.is_recording():
            return
        cfg = self.cfg()
        if not os.environ.get("VREC_FAKE_MIC"):
            mics = vrec.list_audio_devices()
            if not mics:
                notify("vrec", "未发现麦克风", "请连接 USB/外置麦克风后重试")
                return
        rec_dir = Path(os.path.expanduser(cfg["recordings_dir"]))
        rec_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(rec_dir / f"rec-{datetime.now():%Y%m%d-%H%M%S}.mp3")
        cmd = vrec._build_ffmpeg_cmd(cfg, out_path, duration, str(cfg["audio_device"]))
        try:
            self.proc = subprocess.Popen(cmd)
        except Exception as e:  # noqa: BLE001
            notify("vrec", "录音启动失败", str(e)[:120])
            return
        vrec._write_pidfile(self.proc.pid, out_path)
        self.current_file = out_path
        self.record_started = datetime.now()
        vrec.log(f"[app] recording -> {out_path}", quiet=True)
        notify("vrec", "开始录音", os.path.basename(out_path))

    def stop_recording(self):
        if self.is_recording():
            try:
                self.proc.send_signal(signal.SIGINT)  # let ffmpeg finalize
            except ProcessLookupError:
                pass

    def _on_finished(self):
        vrec._clear_pidfile()
        path = self.current_file
        self.current_file = None
        if not path or not Path(path).exists() or Path(path).stat().st_size < 1024:
            notify("vrec", "录音为空", "请检查麦克风权限与设备选择")
            return
        notify("vrec", "录音已保存", os.path.basename(path))
        if self.auto_transcribe:
            self.transcribe_async(path)

    # ------------------------------------------------------------------ #
    # transcription
    # ------------------------------------------------------------------ #
    def transcribe_async(self, path):
        if self.transcribing:
            notify("vrec", "请稍候", "正在转译上一段音频")
            return

        def on_progress(frac, detail):
            self.tx_detail = detail or ""

        def work():
            self.transcribing = True
            self.tx_detail = ""
            try:
                cfg = self.cfg()
                text = vrec.transcribe_file(cfg, path, quiet=True, progress=on_progress)
                out = Path(path).with_suffix(".txt")
                out.write_text(text + "\n", encoding="utf-8")
                notify("vrec", "转译完成 ✓", (text[:120] or "（识别为空）"))
                vrec.log(f"[app] transcript -> {out}", quiet=True)
            except Exception as e:  # noqa: BLE001
                notify("vrec", "转译失败", str(e)[:160])
            finally:
                self.transcribing = False
                self.tx_detail = ""

        threading.Thread(target=work, daemon=True).start()

    def tx_last(self, _):
        path = vrec._latest_recording(self.cfg())
        if not path:
            notify("vrec", "没有录音", "录音目录为空")
            return
        self.transcribe_async(path)

    def tx_file(self, _):
        path = pick_audio_file()
        if path:
            self.transcribe_async(path)

    def toggle_auto(self, sender):
        sender.state = 0 if sender.state else 1
        self.auto_transcribe = bool(sender.state)

    # ------------------------------------------------------------------ #
    # timed / scheduled
    # ------------------------------------------------------------------ #
    def _timed(self, secs):
        return lambda _: self.start_recording(duration=secs)

    def timed_custom(self, _):
        d = ask("录音时长（如 30m, 1h30m, 90s）", "30m")
        if not d:
            return
        try:
            self.start_recording(duration=vrec.parse_duration(d))
        except ValueError as e:
            notify("vrec", "格式错误", str(e))

    def schedule_record(self, _):
        s = ask("开始时间（HH:MM 或 2026-06-22 09:00）", f"{datetime.now():%H:%M}")
        if not s:
            return
        d = ask("时长（如 30m, 1h；留空=手动停止）", "30m")
        try:
            start_dt = vrec.parse_when(s)
            dur = vrec.parse_duration(d) if d else None
        except ValueError as e:
            notify("vrec", "格式错误", str(e))
            return
        self.scheduled = (start_dt, dur)
        self.item_cancel_sched.set_callback(self.cancel_schedule)
        tail = f"，录 {vrec.fmt_secs(dur)}" if dur else "（手动停止）"
        notify("vrec", "已预约录音", f"{start_dt:%m-%d %H:%M} 开始{tail}")

    def cancel_schedule(self, _):
        self.scheduled = None
        self.item_cancel_sched.set_callback(None)
        notify("vrec", "已取消预约", "")

    # ------------------------------------------------------------------ #
    # engine / model / mic / settings
    # ------------------------------------------------------------------ #
    def set_engine(self, engine):
        cfg = self.cfg()
        cfg["engine"] = engine
        vrec.save_config(cfg)
        self._refresh_engine_state()
        if engine == "local":
            notify("vrec", "已切换到本地模型", "首次使用会自动下载模型")
            threading.Thread(target=self._warm_local, daemon=True).start()
        else:
            notify("vrec", "已切换到云端模型", "Aliyun")

    def _warm_local(self):
        try:
            name = self.cfg().get("local_model") or "small"
            if name not in vrec._LOCAL_MODELS:
                notify("vrec", "准备本地模型", f"正在加载/下载 {name}…")
                vrec.load_local_model(name)
                notify("vrec", "本地模型就绪 ✓", name)
        except Exception as e:  # noqa: BLE001
            notify("vrec", "本地模型准备失败", str(e)[:160])

    def _refresh_engine_state(self):
        eng = self.cfg().get("engine", "cloud")
        self.item_engine_cloud.state = 1 if eng == "cloud" else 0
        self.item_engine_local.state = 1 if eng == "local" else 0

    def _set_model(self, name):
        def cb(_):
            cfg = self.cfg()
            cfg["local_model"] = name
            vrec.save_config(cfg)
            self._refresh_model_state()
            if cfg.get("engine") == "local":
                threading.Thread(target=self._warm_local, daemon=True).start()
        return cb

    def _refresh_model_state(self):
        cur = self.cfg().get("local_model", "small")
        for name, it in self.model_items.items():
            it.state = 1 if name == cur else 0

    def _refresh_mics(self):
        for key in list(self.mic_menu.keys()):
            del self.mic_menu[key]
        mics = vrec.list_audio_devices()
        cur = str(self.cfg().get("audio_device"))
        if not mics:
            self.mic_menu.add(rumps.MenuItem("（未发现输入设备）"))
        else:
            for idx, name in mics:
                tag = "🔊" if vrec.device_kind(name) == "system" else "🎙"
                it = rumps.MenuItem(f"{tag} [{idx}] {name}", callback=self._set_mic(idx))
                it.state = 1 if idx == cur else 0
                self.mic_menu.add(it)
        self.mic_menu.add(rumps.separator)
        self.mic_menu.add(rumps.MenuItem("🔊 录制系统声音(扬声器)…",
                                         callback=self.system_audio_help))
        self.mic_menu.add(rumps.MenuItem("刷新设备列表",
                                         callback=lambda _: self._refresh_mics()))

    def system_audio_help(self, _):
        has = any(vrec.device_kind(n) == "system" for _, n in vrec.list_audio_devices())
        if has:
            rumps.alert(
                "录制系统声音 / 扬声器",
                "已检测到回环设备。在『录音设备』里选择带 🔊 的设备即可录扬声器/系统声音。\n\n"
                "想同时还能听见声音：打开『音频 MIDI 设置』新建一个『多输出设备』，"
                "勾选 你的显示器 + BlackHole，并把它设为系统输出。")
        else:
            rumps.alert(
                "录制系统声音需要回环设备",
                "macOS 不能直接录扬声器输出，需要先安装虚拟回环设备 BlackHole：\n\n"
                "    brew install blackhole-2ch\n\n"
                "安装后 BlackHole 会作为输入设备出现在『录音设备』里，选它即可录系统声音。\n\n"
                "想同时还能听见声音：在『音频 MIDI 设置』新建『多输出设备』= 显示器 + BlackHole，"
                "并设为系统输出。")

    def _set_mic(self, idx):
        def cb(_):
            cfg = self.cfg()
            cfg["audio_device"] = idx
            vrec.save_config(cfg)
            self._refresh_mics()
        return cb

    def set_api_key(self, _):
        cur = self.cfg().get("api_key", "")
        val = ask("阿里云 / OpenAI 兼容 API Key", cur, ok="保存")
        if val is not None:
            cfg = self.cfg(); cfg["api_key"] = val; vrec.save_config(cfg)
            notify("vrec", "已保存 API Key", "")

    def set_base_url(self, _):
        val = ask("Base URL（OpenAI 兼容端点）", self.cfg().get("base_url", ""), ok="保存")
        if val:
            cfg = self.cfg(); cfg["base_url"] = val; vrec.save_config(cfg)
            notify("vrec", "已保存 Base URL", val)

    def set_model_name(self, _):
        val = ask("云端模型名", self.cfg().get("model", ""), ok="保存")
        if val:
            cfg = self.cfg(); cfg["model"] = val; vrec.save_config(cfg)
            notify("vrec", "已保存模型名", val)

    def open_folder(self, _):
        d = Path(os.path.expanduser(self.cfg()["recordings_dir"]))
        d.mkdir(parents=True, exist_ok=True)
        self._open(d)

    # ------------------------------------------------------------------ #
    # custom menu-bar logo
    # ------------------------------------------------------------------ #
    def _apply_logo(self, path):
        try:
            try:
                self.template = False     # show the image in color, not as a mask
            except Exception:             # noqa: BLE001
                pass
            self.icon = path
            self.has_logo = True
        except Exception as e:            # noqa: BLE001
            notify("vrec", "图标设置失败", str(e)[:120])

    def _apply_logo_from_config(self):
        lp = os.path.expanduser(self.cfg().get("logo_path") or "")
        if lp and os.path.exists(lp):
            self._apply_logo(lp)

    def set_logo(self, _):
        src = pick_image_file()
        if not src:
            return
        dst = str(vrec.APP_DIR / "logo.png")
        try:
            vrec.make_icon(src, dst, size=44)
        except Exception as e:            # noqa: BLE001
            notify("vrec", "图标处理失败", str(e)[:140])
            return
        cfg = self.cfg(); cfg["logo_path"] = dst; vrec.save_config(cfg)
        self._apply_logo(dst)
        notify("vrec", "已更新菜单栏图标 ✓", "")

    def clear_logo(self, _):
        cfg = self.cfg(); cfg["logo_path"] = ""; vrec.save_config(cfg)
        self.has_logo = False
        try:
            self.icon = None
        except Exception:                 # noqa: BLE001
            pass
        self.title = IDLE_ICON
        notify("vrec", "已恢复默认图标", "")

    # ------------------------------------------------------------------ #
    # main loop tick
    # ------------------------------------------------------------------ #
    def tick(self, _):
        # finished recording? (process exited since last tick)
        if self.proc is not None and self.proc.poll() is not None:
            self.proc = None
            self._on_finished()

        if self.is_recording():
            elapsed = (datetime.now() - self.record_started).total_seconds()
            clock = vrec.fmt_clock(elapsed)
            self.title = f"{REC_ICON} {clock}"            # always visible in the menu bar
            self.item_record.title = "■ 停止录音"
            name = os.path.basename(self.current_file or "")
            self.status_item.title = f"🔴 录音中 · {clock} · {name}"
        elif self.transcribing:
            d = self.tx_detail
            self.title = f"{BUSY_ICON} {d}".rstrip()
            self.item_record.title = "● 开始录音"
            self.status_item.title = f"💬 转译中 {d}".rstrip() if d else "💬 正在转译…"
        elif self.scheduled:
            start_dt, dur = self.scheduled
            self.title = "⏰" if self.has_logo else f"{IDLE_ICON} ⏰"
            self.item_record.title = "● 开始录音"
            tail = f"，录 {vrec.fmt_secs(dur)}" if dur else "（手动停止）"
            self.status_item.title = f"⏰ 已预约 {start_dt:%m-%d %H:%M}{tail}"
            if datetime.now() >= start_dt:
                self.scheduled = None
                self.item_cancel_sched.set_callback(None)
                self.start_recording(duration=dur)
        else:
            self.title = "" if self.has_logo else IDLE_ICON
            self.item_record.title = "● 开始录音"
            self.status_item.title = "⚪ 待机"

    def quit_app(self, _):
        if self.is_recording():
            self.stop_recording()
            try:
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        rumps.quit_application()


def main():
    # menu-bar-only: no Dock icon (like ClashX)
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory)
    except Exception:
        pass
    VrecApp().run()


if __name__ == "__main__":
    main()

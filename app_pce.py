# app_pce.py
# -*- coding: utf-8 -*-
"""PCE PSG 实时播放器 —— 图形界面版（tkinter，零额外依赖）。

    python app_pce.py                 # 打开窗口
    python app_pce.py --lang en       # 英文界面
    python app_pce.py --selfcheck     # 不开窗口，检查运行环境（EXE 打包后可用）
    python app_pce.py --play x.mid    # 不开窗口，直接播一遍（排错用）

和 OPMMIDI-RT 的 app.py 同构：同样的语言切换、循环、导出 WAV、状态栏；
多出来的是"芯片数 / 音色风格 / 输出精度"三组模式，以及进度条点击跳转。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from pcepsg_session import (BASE, CHIP_CHOICES, DIV_CHOICES, DRUM_MODES, HERE,
                            STYLES, Config, Session)

APP_TITLE = "PCE PSG Player"

STRINGS = {
    "zh": {
        "title": "PCE PSG 实时播放器",
        "midi": "MIDI",
        "chips": "芯片模式",
        "style": "音色风格",
        "drums": "鼓",
        "div": "输出精度",
        "volume": "音量",
        "volume_hint": "（播放中实时生效）",
        "browse": "浏览…",
        "play": "▶ 播放",
        "pause": "⏸ 暂停",
        "resume": "▶ 继续",
        "stop": "■ 停止",
        "export": "导出 WAV…",
        "export_t": "导出 WAV",
        "export_done": "已导出：\n{path}\n\n{sr} Hz / {sec:.1f}s / peak {peak}/32767",
        "export_cancel": "导出已取消",
        "export_busy": "正在导出 {pct}%…",
        "loop": "循环播放",
        "lang": "语言",
        "ch": "{n} 芯片 · {c} 通道",
        "style_standard": "标准（方波为主）",
        "style_soft": "柔和（正弦/三角）",
        "style_bright": "锐利（脉冲/锯齿）",
        "drum_auto": "自动（有通道 9 就开）",
        "drum_on": "开（扫频底鼓 + 噪声）",
        "drum_off": "关（全部给旋律）",
        "div_16": "16 分频（精细，推荐）",
        "div_64": "64 分频（轻量）",
        "err_no_midi": "先选一个 MIDI 文件。",
        "err_no_midi_t": "缺少 MIDI",
        "err_play": "播放失败",
        "status_playing": "{name} | {c} 芯片 {ch} 通道 | 鼓 {drums} | {n} 事件 {sec:.1f}s",
        "xrun": "underrun={u}  xrun={x}",
        "ready": "就绪：选一个 MIDI 然后点播放",
        "hint_seek": "（点击进度条可跳转）",
    },
    "en": {
        "title": "PCE PSG Realtime Player",
        "midi": "MIDI",
        "chips": "Chip mode",
        "style": "Voice style",
        "drums": "Drums",
        "div": "Precision",
        "volume": "Volume",
        "volume_hint": "(applies live)",
        "browse": "Browse…",
        "play": "▶ Play",
        "pause": "⏸ Pause",
        "resume": "▶ Resume",
        "stop": "■ Stop",
        "export": "Export WAV…",
        "export_t": "Export WAV",
        "export_done": "Exported:\n{path}\n\n{sr} Hz / {sec:.1f}s / peak {peak}/32767",
        "export_cancel": "Export cancelled",
        "export_busy": "Exporting {pct}%…",
        "loop": "Loop playback",
        "lang": "Language",
        "ch": "{n} chip(s) · {c} channels",
        "style_standard": "Standard (square-ish)",
        "style_soft": "Soft (sine/triangle)",
        "style_bright": "Bright (pulse/saw)",
        "drum_auto": "Auto (on if channel 9 exists)",
        "drum_on": "On (swept kick + noise)",
        "drum_off": "Off (all channels melodic)",
        "div_16": "÷16 (precise, recommended)",
        "div_64": "÷64 (lightweight)",
        "err_no_midi": "Pick a MIDI file first.",
        "err_no_midi_t": "Missing MIDI",
        "err_play": "Playback failed",
        "status_playing": "{name} | {c} chips {ch} ch | drums {drums} | {n} events {sec:.1f}s",
        "xrun": "underrun={u}  xrun={x}",
        "ready": "Ready: pick a MIDI, then press Play",
        "hint_seek": "(click the bar to seek)",
    },
}


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lang = cfg.lang if cfg.lang in STRINGS else "zh"
        self.session: Session | None = None
        self.paused = False
        self._exp = {"running": False, "frac": 0.0, "done": None}
        self._exp_cancel = threading.Event()
        self._seeking = False
        self.root = tk.Tk()
        self.root.geometry("820x520")
        self.root.minsize(760, 500)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll()

    # ---------------- i18n ----------------
    def t(self, key: str) -> str:
        return STRINGS[self.lang].get(key, key)

    def style_label(self, s: str) -> str:
        return self.t(f"style_{s}")

    def drum_label(self, d: str) -> str:
        return self.t(f"drum_{d}")

    def div_label(self, d: int) -> str:
        return self.t(f"div_{d}")

    def _style_from_label(self, label: str) -> str:
        for s in STYLES:
            if self.style_label(s) == label:
                return s
        return "standard"

    def _drum_from_label(self, label: str) -> str:
        for d in DRUM_MODES:
            if self.drum_label(d) == label:
                return d
        return "auto"

    def _div_from_label(self, label: str) -> int:
        for d in DIV_CHOICES:
            if self.div_label(d) == label:
                return d
        return 16

    # ---------------- UI ----------------
    def _build(self):
        self.root.title(f"{self.t('title')}  ·  {os.path.basename(self.cfg.midi) or ''}")
        if getattr(self, "frm", None) is not None:
            self.frm.destroy()
        if getattr(self, "status_bar", None) is not None:
            self.status_bar.destroy()

        pad = {"padx": 6, "pady": 4}
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        self.frm = frm

        def row(r, text):
            ttk.Label(frm, text=text).grid(row=r, column=0, sticky="w", **pad)

        # MIDI
        row(0, self.t("midi"))
        self.v_midi = tk.StringVar(value=self.cfg.midi)
        ttk.Entry(frm, textvariable=self.v_midi).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(frm, text=self.t("browse"), command=self.pick_midi).grid(row=0, column=2, **pad)

        # 芯片模式
        row(1, self.t("chips"))
        self.v_chips = tk.IntVar(value=self.cfg.chips)
        bar = ttk.Frame(frm)
        bar.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        for n in CHIP_CHOICES:
            ttk.Radiobutton(bar, text=self.t("ch").format(n=n, c=n * 6),
                            value=n, variable=self.v_chips,
                            command=self.on_chips).pack(side="left", padx=4)

        # 音色风格
        row(2, self.t("style"))
        self.v_style = tk.StringVar(value=self.style_label(self.cfg.style))
        scb = ttk.Combobox(frm, textvariable=self.v_style, state="readonly",
                           values=[self.style_label(s) for s in STYLES])
        scb.grid(row=2, column=1, columnspan=2, sticky="ew", **pad)
        scb.bind("<<ComboboxSelected>>", lambda _e: self.on_style())

        # 鼓
        row(3, self.t("drums"))
        self.v_drums = tk.StringVar(value=self.drum_label(self.cfg.drums))
        dcb = ttk.Combobox(frm, textvariable=self.v_drums, state="readonly",
                           values=[self.drum_label(d) for d in DRUM_MODES])
        dcb.grid(row=3, column=1, columnspan=2, sticky="ew", **pad)
        dcb.bind("<<ComboboxSelected>>", lambda _e: self.on_drums())

        # 输出精度
        row(4, self.t("div"))
        self.v_div = tk.StringVar(value=self.div_label(self.cfg.div))
        vcb = ttk.Combobox(frm, textvariable=self.v_div, state="readonly",
                           values=[self.div_label(d) for d in DIV_CHOICES])
        vcb.grid(row=4, column=1, columnspan=2, sticky="ew", **pad)
        vcb.bind("<<ComboboxSelected>>", lambda _e: self.on_div())

        # 音量
        row(5, self.t("volume"))
        self.v_vol = tk.DoubleVar(value=self.cfg.volume)
        ttk.Scale(frm, from_=0.0, to=1.5, variable=self.v_vol,
                  command=self.on_volume).grid(row=5, column=1, sticky="ew", **pad)
        ttk.Label(frm, text=self.t("volume_hint")).grid(row=5, column=2, sticky="w", **pad)

        # 语言
        row(6, self.t("lang"))
        self.v_lang = tk.StringVar(value="中文" if self.lang == "zh" else "English")
        lcb = ttk.Combobox(frm, textvariable=self.v_lang, state="readonly",
                           values=["中文", "English"], width=10)
        lcb.grid(row=6, column=1, sticky="w", **pad)
        lcb.bind("<<ComboboxSelected>>", lambda _e: self.on_lang())

        # 控制
        ctl = ttk.Frame(frm)
        ctl.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(10, 2))
        self.btn_play = ttk.Button(ctl, text=self.t("play"), command=self.on_play)
        self.btn_play.pack(side="left", padx=4)
        self.btn_pause = ttk.Button(ctl, text=self.t("pause"), command=self.on_pause,
                                    state="disabled")
        self.btn_pause.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(ctl, text=self.t("stop"), command=self.on_stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=4)
        self.btn_export = ttk.Button(ctl, text=self.t("export"), command=self.on_export)
        self.btn_export.pack(side="left", padx=4)

        # 循环
        self.v_loop = tk.BooleanVar(value=self.cfg.loop)
        ttk.Checkbutton(frm, text=self.t("loop"), variable=self.v_loop,
                        command=self.on_loop).grid(row=8, column=0, columnspan=3,
                                                   sticky="w", **pad)

        # 进度（可点击跳转）
        self.v_prog = tk.DoubleVar(value=0.0)
        self.pbar = ttk.Progressbar(frm, variable=self.v_prog, maximum=100)
        self.pbar.grid(row=9, column=0, columnspan=3, sticky="ew", **pad)
        self.pbar.bind("<Button-1>", self.on_seek_click)
        self.pbar.bind("<B1-Motion>", self.on_seek_click)

        self.v_time = tk.StringVar(value="0.0 / 0.0 s")
        ttk.Label(frm, textvariable=self.v_time).grid(row=10, column=0, columnspan=2,
                                                      sticky="w", **pad)
        ttk.Label(frm, text=self.t("hint_seek")).grid(row=10, column=2, sticky="e", **pad)

        # 状态栏
        self.v_status = tk.StringVar(value=self.t("ready"))
        bar2 = ttk.Label(self.root, textvariable=self.v_status, relief="sunken",
                         anchor="w", padding=4)
        bar2.pack(fill="x", side="bottom")
        self.status_bar = bar2

    # ---------------- 交互 ----------------
    def pick_midi(self):
        p = filedialog.askopenfilename(
            initialdir=os.path.dirname(self.v_midi.get()) or BASE,
            filetypes=[("MIDI", "*.mid *.midi"), ("All", "*.*")])
        if p:
            self.v_midi.set(p)
            self.cfg.midi = p
            self.root.title(f"{self.t('title')}  ·  {os.path.basename(p)}")

    def on_chips(self):
        self.cfg.chips = int(self.v_chips.get())

    def on_style(self):
        self.cfg.style = self._style_from_label(self.v_style.get())

    def on_drums(self):
        self.cfg.drums = self._drum_from_label(self.v_drums.get())

    def on_div(self):
        self.cfg.div = self._div_from_label(self.v_div.get())

    def on_volume(self, _v=None):
        self.cfg.volume = float(self.v_vol.get())
        if self.session:
            self.session.set_volume(self.cfg.volume)

    def on_loop(self):
        self.cfg.loop = bool(self.v_loop.get())
        self.cfg.save()

    def on_lang(self):
        self.lang = "en" if self.v_lang.get() == "English" else "zh"
        self.cfg.lang = self.lang
        self.cfg.save()
        self._build()

    def on_seek_click(self, ev):
        s = self.session
        if not s or s.total_sec <= 0 or self._exp["running"]:
            return
        frac = max(0.0, min(1.0, ev.x / max(1, self.pbar.winfo_width())))
        try:
            self._seeking = True
            s.seek(frac * s.total_sec)
        except Exception:
            pass
        finally:
            self._seeking = False

    # ---------------- 播放 ----------------
    def _cfg_from_ui(self) -> Config:
        c = Config(**{k: getattr(self.cfg, k) for k in self.cfg.__dataclass_fields__})
        c.midi = self.v_midi.get().strip()
        c.chips = int(self.v_chips.get())
        c.style = self._style_from_label(self.v_style.get())
        c.drums = self._drum_from_label(self.v_drums.get())
        c.div = self._div_from_label(self.v_div.get())
        c.volume = float(self.v_vol.get())
        c.lang = self.lang
        c.loop = bool(self.v_loop.get())
        return c

    def _set_playing_ui(self, playing: bool, paused: bool = False):
        self.btn_play["state"] = "disabled" if playing else "normal"
        self.btn_stop["state"] = "normal" if playing else "disabled"
        self.btn_pause["state"] = "normal" if playing else "disabled"
        self.btn_pause["text"] = self.t("resume") if paused else self.t("pause")

    def on_play(self):
        cfg = self._cfg_from_ui()
        if not cfg.midi or not os.path.exists(cfg.midi):
            messagebox.showerror(self.t("err_no_midi_t"), self.t("err_no_midi"))
            return
        self.on_stop()
        try:
            self.session = Session(cfg)
            self.session.build()
            self.session.start()
        except Exception as e:
            self.session = None
            messagebox.showerror(self.t("err_play"),
                                 f"{type(e).__name__}: {e}\n\n{traceback.format_exc(limit=2)}")
            return
        self.cfg = cfg
        cfg.save()
        self.paused = False
        self._set_playing_ui(True)
        s = self.session
        self.v_status.set(self.t("status_playing").format(
            name=os.path.basename(cfg.midi), c=cfg.chips, ch=s.channels,
            drums="on" if s.drums else "off", n=s.n_events, sec=s.total_sec))

    def on_pause(self):
        s = self.session
        if not s or not s.stream:
            return
        try:
            if not self.paused:
                s.stream.stop()
                self.paused = True
            else:
                s.stream.start()
                self.paused = False
        except Exception:
            pass
        self._set_playing_ui(True, self.paused)

    def on_stop(self):
        if self._exp["running"]:
            self._exp_cancel.set()
        if self.session:
            self.session.close()
            self.session = None
        self.paused = False
        self._set_playing_ui(False)
        self.v_prog.set(0.0)

    # ---------------- 导出 WAV ----------------
    def on_export(self):
        cfg = self._cfg_from_ui()
        if not cfg.midi or not os.path.exists(cfg.midi):
            messagebox.showerror(self.t("err_no_midi_t"), self.t("err_no_midi"))
            return
        if self._exp["running"]:
            return
        default = os.path.splitext(os.path.basename(cfg.midi))[0] + ".pce.wav"
        path = filedialog.asksaveasfilename(
            defaultextension=".wav", initialfile=default,
            filetypes=[("WAV", "*.wav")],
            initialdir=os.path.dirname(cfg.midi) or BASE)
        if not path:
            return

        self.on_stop()
        self.cfg = cfg
        cfg.save()
        self._exp.update(running=True, frac=0.0, done=None)
        self._exp_cancel.clear()
        self.btn_play["state"] = "disabled"
        self.btn_export["state"] = "disabled"
        threading.Thread(target=self._export_worker, args=(cfg, path), daemon=True).start()

    def _export_worker(self, cfg, path):
        from pcepsg_session import render_wav
        try:
            info = render_wav(cfg, path,
                              progress=lambda f: self._exp.__setitem__("frac", f),
                              cancelled=self._exp_cancel.is_set)
            self._exp["done"] = info
        except Exception as e:
            self._exp["done"] = {"error": f"{type(e).__name__}: {e}"}
        finally:
            self._exp["running"] = False

    def _poll_export(self):
        if self._exp["running"]:
            self.v_prog.set(self._exp["frac"] * 100)
            self.v_status.set(self.t("export_busy").format(pct=int(self._exp["frac"] * 100)))
            return
        info = self._exp.get("done")
        if not info:
            return
        self._exp["done"] = None
        self.btn_play["state"] = "normal"
        self.btn_export["state"] = "normal"
        if "error" in info:
            messagebox.showerror(self.t("export_t"), info["error"])
        elif not info.get("complete"):
            messagebox.showinfo(self.t("export_t"), self.t("export_cancel"))
        else:
            messagebox.showinfo(self.t("export_t"), self.t("export_done").format(
                path=info["path"], sr=info["sr"], sec=info["seconds"], peak=info["peak"]))
        self.v_prog.set(0.0)
        self.v_status.set(self.t("ready"))

    def _poll(self):
        self._poll_export()
        s = self.session
        if s and s.total_sec > 0 and not self._exp["running"] and not self._seeking:
            pos = min(s.position, s.total_sec)
            self.v_prog.set(min(100.0, pos / s.total_sec * 100))
            self.v_time.set(f"{pos:.1f} / {s.total_sec:.1f} s   "
                            + self.t("xrun").format(u=s.underruns, x=s.xruns))
            if s.finished and not self.paused:
                if self.cfg.loop:
                    try:
                        s.seek(0.0)
                        self._set_playing_ui(True)
                    except Exception:
                        self.on_stop()
                else:
                    self.on_stop()
        self.root.after(120, self._poll)

    def on_close(self):
        self.on_stop()
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


# ---------------- 自检（EXE 打包后也能跑）----------------
def selfcheck() -> int:
    lines = []
    ok = True

    def log(s):
        lines.append(str(s))

    log(f"frozen={getattr(sys, 'frozen', False)}")
    log(f"BASE={BASE}")
    log(f"APP_DIR={HERE}")
    log(f"python={sys.version.split()[0]}")

    dll = os.path.join(HERE, "pcepsg.dll")
    log(f"pcepsg.dll: {'OK' if os.path.exists(dll) else 'MISSING'} ({dll})")
    ok &= os.path.exists(dll)

    try:
        from pcepsg_core import PcePsgBank
        b = PcePsgBank(chips=2)
        y = b.generate(1024)
        log(f"PSG bank: OK sr={b.sample_rate():.1f} chips={b.chips()} "
            f"frames={y.shape[0]} peak={int(abs(y).max())}")
        b.close()
    except Exception as e:
        log(f"PSG bank: FAIL {e}")
        ok = False

    try:
        from smf import parse_smf
        cfg = Config.load()
        if cfg.midi and os.path.exists(cfg.midi):
            ev, _t, total = parse_smf(open(cfg.midi, "rb").read())
            log(f"MIDI: OK {os.path.basename(cfg.midi)} {len(ev)} events {total:.1f}s")
        else:
            log("MIDI: (no file configured)")
    except Exception as e:
        log(f"MIDI: FAIL {e}")

    try:
        import sounddevice as sd
        outs = [d for d in sd.query_devices() if d["max_output_channels"] > 0]
        log(f"sounddevice: OK, {len(outs)} output devices, default={sd.default.device}")
    except Exception as e:
        log(f"sounddevice: FAIL {e}")
        ok = False

    text = "\n".join(lines)
    p = os.path.join(BASE, "selfcheck.log")
    try:
        open(p, "w", encoding="utf-8").write(text + "\n")
    except OSError:
        pass
    try:
        print(text)
    except Exception:
        pass
    return 0 if ok else 1


# ---------------- 无界面播放（排错 / 批量试听）----------------
def play_cli(cfg: Config, seconds: float | None = None) -> int:
    s = Session(cfg)
    s.build()
    s.start()
    t0 = time.time()
    try:
        while True:
            time.sleep(0.05)
            if seconds is not None and time.time() - t0 > seconds:
                break
            if s.finished:
                break
    except KeyboardInterrupt:
        pass
    finally:
        pos = s.position
        ur, xr = s.underruns, s.xruns
        s.close()
    print(f"played {pos:.1f}s  underrun={ur} xruns={xr}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--lang", choices=["zh", "en"], default=None)
    ap.add_argument("--play", metavar="MIDI", default=None)
    ap.add_argument("--chips", type=int, default=None)
    ap.add_argument("--sec", type=float, default=None, help="--play 时只播 N 秒")
    args = ap.parse_args(argv)

    if args.selfcheck:
        return selfcheck()

    cfg = Config.load()
    if args.lang:
        cfg.lang = args.lang
    if args.play:
        cfg.midi = args.play
        if args.chips:
            cfg.chips = args.chips
        return play_cli(cfg, args.sec)

    App(cfg).run()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())

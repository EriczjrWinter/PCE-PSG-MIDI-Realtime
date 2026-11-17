# pcepsg_session.py
"""一次播放所需的全部组装：PSG 机架 + 驱动 + 渲染器 + 引擎 + 声卡。

GUI（app_pce.py）和命令行都用这里，避免两份逻辑漂移；离线导出 `render_wav()`
也走同一条链，保证"导出的 WAV"和"听到的声音"是同一个东西。

和 OPM 项目的 player_session.py 是同构的，差别在于：
  * 芯⽚是"机架"（N 颗 PSG），所以复音数可配；
  * seek 不做状态重放的花活，直接重建 + 把按住音重新起音。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import wave
from dataclasses import asdict, dataclass

import numpy as np

import pcew
from pcepsg_core import DIV_DEFAULT, PcePsgBank
from psg_driver import PsgDriver
from psg_sched import Engine, PsgRenderer, Resampler, apply_master
from smf import parse_smf

SR_OUT = 48_000
CHIP_CHOICES = (1, 2, 4, 6)          # 通道数 = 6 × N
DIV_CHOICES = (16, 64)
DRUM_MODES = ("auto", "on", "off")
STYLES = pcew.STYLES

# 固定的补偿增益：PSG 的音量表按 6 通道满幅标定，单声道本来就只有 -16 dBFS，
# 再叠上力度衰减，听起来会偏小。乘一个固定值 + 软限幅，比"每首歌自动归一"更可预期。
MAKEUP = 4.0


def app_dir() -> str:
    """脚本目录；PyInstaller 冻结后指向解包目录（DLL 在那里）。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def work_dir() -> str:
    """配置/输出目录的基准：EXE 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


HERE = app_dir()
BASE = work_dir()
SEARCH_DIRS = [BASE] + ([HERE] if os.path.abspath(HERE) != os.path.abspath(BASE) else [])


@dataclass
class Config:
    midi: str = ""
    chips: int = 4                   # 1 | 2 | 4 | 6
    div: int = DIV_DEFAULT           # 16（精细） | 64（轻量）
    drums: str = "auto"              # auto | on | off
    style: str = "standard"          # standard | soft | bright
    volume: float = 1.0
    lang: str = "zh"                 # zh | en
    loop: bool = False
    tail: float = 1.5
    device: int | None = None
    blocksize: int = 256

    # ---------- 持久化 ----------
    @staticmethod
    def path() -> str:
        return os.path.join(BASE, "player_pce.json")

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        p = cls.path()
        if os.path.exists(p):
            try:
                data = json.load(open(p, encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except Exception:
                pass
        if cfg.chips not in CHIP_CHOICES:
            cfg.chips = 4
        if cfg.div not in DIV_CHOICES:
            cfg.div = DIV_DEFAULT
        if cfg.drums not in DRUM_MODES:
            cfg.drums = "auto"
        if cfg.style not in STYLES:
            cfg.style = "standard"
        return cfg

    def save(self) -> None:
        try:
            json.dump(asdict(self), open(self.path(), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        except OSError:
            pass


class Session:
    """组装好但还没出声的一次播放。"""

    xruns = 0

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.core = None
        self.driver = None
        self.renderer = None
        self.engine = None
        self.stream = None
        self.total_sec = 0.0
        self.n_events = 0
        self.channels = 0
        self.drums = False
        self._events = None
        self._offset = 0.0
        self.xruns = 0

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _master(self) -> float:
        return max(0.0, float(self.cfg.volume)) * MAKEUP

    def build(self) -> None:
        cfg = self.cfg
        if not cfg.midi or not os.path.exists(cfg.midi):
            raise FileNotFoundError(f"找不到 MIDI: {cfg.midi}")

        pcew.set_style(cfg.style)

        events, _tpq, total_sec = parse_smf(open(cfg.midi, "rb").read())
        self._events = events
        self.total_sec = total_sec
        self.n_events = len(events)

        drums = cfg.drums == "on" or (cfg.drums == "auto" and any(e.ch == 9 for e in events))
        self.drums = drums

        self.core = PcePsgBank(chips=cfg.chips, div=cfg.div)
        self.core.set_gain(1.0 / math.sqrt(cfg.chips))
        self.driver = PsgDriver(self.core, drums=drums, chips=cfg.chips)
        self.channels = cfg.chips * 6
        self.renderer = PsgRenderer(self.core, self.driver, events,
                                    total_sec=total_sec, tail=cfg.tail)
        self.driver.scheduler = self.renderer.schedule_note
        self._offset = 0.0
        self._new_engine()

    def _new_engine(self) -> None:
        self.engine = Engine(self.renderer, sr_out=SR_OUT,
                             blocksize=self.cfg.blocksize, target_blocks=8, chip_chunk=1024,
                             master=self._master())

    # ------------------------------------------------------------------
    # 播放
    # ------------------------------------------------------------------
    def start(self, device=None) -> None:
        import sounddevice as sd
        dev = device if device is not None else self.cfg.device
        self.stream = sd.OutputStream(device=dev, samplerate=SR_OUT, channels=2,
                                      dtype="int16", blocksize=self.cfg.blocksize,
                                      callback=self._callback)
        self.engine.start()
        # 预填一点，避免开局 underrun
        t0 = time.time()
        while (self.engine.buffered_frames() < self.cfg.blocksize * 6
               and time.time() - t0 < 2.0):
            time.sleep(0.005)
        self.stream.start()

    def _callback(self, outdata, frames, time_info, status):
        if status:
            self.xruns += 1
        outdata[:] = self.engine.pull(frames)

    def stop(self) -> None:
        if getattr(self, "stream", None):
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        if self.engine:
            self.engine.stop()

    def close(self) -> None:
        self.stop()
        if self.core is not None:
            try:
                self.core.close()
            except Exception:
                pass
            self.core = None

    # ------------------------------------------------------------------
    # 跳转
    # ------------------------------------------------------------------
    def _event_index_at(self, frame: int) -> int:
        ev = self.renderer.ev
        lo, hi = 0, len(ev)
        while lo < hi:
            mid = (lo + hi) // 2
            if ev[mid][0] < frame:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _replay_to(self, frame: int) -> None:
        """把 frame 之前的事件重放成驱动状态，再把仍按住的音重新起音。

        PSG 是有状态芯片（波形指针 / 分频计数器 / 软件包络），没法真的"跳到中间"。
        这里不追求包络相位精确，只保证：音色、弯音、通道音量、声像、
        以及"此刻还按着哪些音"是对的。听起来就是从中间接上。
        """
        drv = self.driver
        sr = self.core.sample_rate()
        held: dict[tuple[int, int], int] = {}
        order: list[tuple[int, int]] = []

        for e in self._events:
            if int(round(e.t * sr)) > frame:
                break
            if e.kind == "prog":
                drv.program_change(0, e.ch, e.d1)
            elif e.kind == "cc":
                drv.control_change(0, e.ch, e.d1, e.d2)
            elif e.kind == "bend":
                drv.pitch_bend(0, e.ch, e.d1, e.d2)
            elif e.kind == "on":
                key = (e.ch, e.d1)
                if e.d2 > 0:
                    if key not in held:
                        order.append(key)
                    held[key] = e.d2
                else:
                    held.pop(key, None)
            elif e.kind == "off":
                held.pop((e.ch, e.d1), None)

        for ch, note in order:
            if (ch, note) in held:
                drv.note_on(frame, ch, note, held[(ch, note)])

    def seek(self, seconds: float) -> None:
        """跳到指定时刻并继续播放（保持流不关）。"""
        if not self._events:
            return
        t = max(0.0, min(float(seconds), self.total_sec))
        playing = self.stream is not None
        self.stop()
        self.close()

        self.build()
        frame = int(t * self.core.sample_rate())
        self._replay_to(frame)
        self.renderer.frame = frame
        self.renderer.ei = self._event_index_at(frame)
        self._new_engine()
        self._offset = t
        if playing:
            self.start()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def finished(self) -> bool:
        return self.engine is not None and self.engine.finished()

    @property
    def position(self) -> float:
        if not self.engine:
            return self._offset
        return self._offset + self.engine.emitted / float(SR_OUT)

    @property
    def underruns(self) -> int:
        return self.engine.underruns if self.engine else 0

    def set_volume(self, v: float) -> None:
        self.cfg.volume = float(v)
        if self.engine is not None:
            self.engine.master = self._master()


# ---------------------------------------------------------------------------
# 离线导出：和实时播放同一条链（机架增益 / 总音量 / 软限幅都一样）
# ---------------------------------------------------------------------------
def render_wav(cfg: Config, out_path: str, sr_out: int = SR_OUT,
               progress=None, cancelled=None) -> dict:
    s = Session(cfg)
    s.build()
    try:
        chip_sr = s.core.sample_rate()
        res = Resampler(chip_sr, sr_out)
        total = max(1, s.renderer.total_frames)
        written = 0
        peak = 0

        with wave.open(out_path, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(sr_out)
            while not s.renderer.done:
                if cancelled is not None and cancelled():
                    break
                blk = res.process(s.renderer.render(512))
                if blk.shape[0]:
                    blk = apply_master(blk, s._master())
                    w.writeframes(blk.tobytes())
                    written += blk.shape[0]
                    peak = max(peak, int(np.abs(blk).max()))
                if progress is not None:
                    progress(min(1.0, s.renderer.frame / total))

        return {
            "path": out_path,
            "sr": sr_out,
            "frames": written,
            "seconds": written / sr_out,
            "peak": peak,
            "chip_sr": chip_sr,
            "channels": s.channels,
            "complete": bool(s.renderer.done),
        }
    finally:
        s.close()

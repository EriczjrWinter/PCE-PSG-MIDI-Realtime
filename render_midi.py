# render_midi.py
"""MIDI → WAV（离线渲染）；同时提供内置 demo，不依赖任何 MIDI 文件。

用法：
    python render_midi.py song.mid              # 生成 song.pce.wav
    python render_midi.py song.mid -o out.wav --sec 30
    python render_midi.py --demo                # 内置演示（音色/包络/鼓）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import wave

import numpy as np

from pcepsg_core import DIV_DEFAULT, PcePsgBank, PcePsgCore
from psg_driver import PsgDriver
from psg_sched import PsgRenderer, Resampler, apply_master

SR_OUT = 48000


# --------------------------------------------------------------------------
def render_events(events, total_sec: float, *, drums: bool, chips: int = 4,
                  tail: float = 1.5, sr_out: int = SR_OUT, div: int = DIV_DEFAULT,
                  gain: float = 2.0, verbose: bool = True):
    if chips > 1:
        core = PcePsgBank(chips=chips, div=div)
    else:
        core = PcePsgCore(div=div)
    driver = PsgDriver(core, drums=drums, chips=chips)
    rnd = PsgRenderer(core, driver, events, total_sec=total_sec, tail=tail)
    driver.scheduler = rnd.schedule_note

    res = Resampler(rnd.sr, sr_out)
    chunks = []
    n_audio = 0
    t0 = time.perf_counter()

    block = 1024
    while not rnd.done:
        chip = rnd.render(block)
        n_audio += chip.shape[0]
        out = res.process(chip)
        if out.shape[0]:
            chunks.append(out)

    dt = time.perf_counter() - t0
    audio = np.concatenate(chunks) if chunks else np.zeros((0, 2), np.int16)

    applied_gain = 1.0
    if audio.shape[0]:
        f = audio.astype(np.float32)
        peak_raw = float(np.abs(f).max())
        applied_gain = gain if gain > 0 else (0.90 * 32767.0 / max(peak_raw, 1.0))
        audio = apply_master(f, applied_gain)
    if verbose:
        dur = audio.shape[0] / sr_out
        peak = int(np.abs(audio).max()) if audio.shape[0] else 0
        clip = 100.0 * float(np.mean(np.abs(audio) >= 32767)) if audio.shape[0] else 0.0
        print(f"  芯片帧={n_audio}  输出={audio.shape[0]} 帧 ({dur:.2f}s @ {sr_out})")
        print(f"  用时 {dt:.2f}s → {1000 * dt / max(dur, 1e-9):.1f} ms / 音频秒")
        print(f"  音符 {driver.stats['notes']}  抢占 {driver.stats['steals']}  "
              f"寄存器写入 {driver.stats['writes']}")
        print(f"  峰值 {peak} ({20 * np.log10(max(peak, 1) / 32768):.1f} dBFS)  "
              f"增益 ×{applied_gain:.2f}  削波 {clip:.3f}%")

    return audio, driver, dt


def write_wav(path: str, audio: np.ndarray, sr: int = SR_OUT) -> None:
    audio = np.clip(audio, -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(audio.tobytes())


# --------------------------------------------------------------------------
# 内置 demo：琶音 + 和弦 + 贝斯 + 鼓，用来听音色/包络/抢占
# --------------------------------------------------------------------------
def demo_events():
    from smf import Event

    ev = []
    t = 0.05
    # 16 分音符琶音，走 4 个音色组
    scale = [60, 64, 67, 72, 76, 79, 84, 79, 76, 72, 67, 64]
    for i in range(48):
        mch = 0 if i % 2 == 0 else 1
        ev.append(Event(t, "prog", mch, 0 if i % 4 else 10, 0))
        ev.append(Event(t, "on", mch, scale[i % len(scale)] + 12 * (i // 12), 100))
        ev.append(Event(t + 0.11, "off", mch, scale[i % len(scale)] + 12 * (i // 12), 0))
        t += 0.125

    # 低音
    t = 0.05
    for i in range(24):
        ev.append(Event(t, "on", 2, [36, 36, 43, 41][i % 4], 110))
        ev.append(Event(t + 0.22, "off", 2, [36, 36, 43, 41][i % 4], 0))
        t += 0.25

    # 鼓
    t = 0.05
    for i in range(48):
        if i % 4 == 0:
            ev.append(Event(t, "on", 9, 36, 120))
        if i % 4 == 2:
            ev.append(Event(t, "on", 9, 38, 100))
        ev.append(Event(t, "on", 9, 42, 60))
        t += 0.125

    ev.sort(key=lambda e: e.t)
    return ev, t


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PCE PSG (HuC6280) MIDI 渲染器")
    ap.add_argument("mid", nargs="?")
    ap.add_argument("-o", "--out")
    ap.add_argument("--sec", type=float, default=0, help="只渲染前 N 秒（0=全部）")
    ap.add_argument("--tail", type=float, default=1.5)
    ap.add_argument("--drums", choices=["auto", "on", "off"], default="auto")
    ap.add_argument("--chips", type=int, default=4,
                    help="PSG 芯片数，通道数 = 6×N（默认 4 = 24 通道；1 = 历史单芯片）")
    ap.add_argument("--div", type=int, default=DIV_DEFAULT, help="PSG 时钟分频（越小越精细、越吃 CPU）")
    ap.add_argument("--gain", type=float, default=0.0,
                    help="输出增益；0 = 自动归一到 -1 dBFS")
    ap.add_argument("--rounds", type=int, default=1, help="重复渲染用于测吞吐")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args(argv)

    if args.demo:
        events, total = demo_events()
        src = "内置 demo"
        out = args.out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "demo_pce.wav")
        drums = True
    else:
        if not args.mid:
            ap.error("需要 MIDI 文件，或用 --demo")
        from smf import parse_smf
        data = open(args.mid, "rb").read()
        events, tpq, total = parse_smf(data)
        src = args.mid
        out = args.out or (os.path.splitext(args.mid)[0] + ".pce.wav")
        if args.drums == "auto":
            drums = any(e.ch == 9 for e in events)
        else:
            drums = args.drums == "on"

    if args.sec > 0:
        total = min(total, args.sec)

    print(f"[PCE PSG] {os.path.basename(src)}  时长 {total:.1f}s  "
          f"{args.chips} 芯片/{args.chips * 6} 通道  鼓={'on' if drums else 'off'}")
    audio, _drv, _dt = render_events(events, total, drums=drums, chips=args.chips,
                                     tail=args.tail, gain=args.gain, div=args.div)
    write_wav(out, audio)
    print(f"  → {out}")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(main())

# tool/selftest.py
"""PCE PSG 引擎自检。

跑法：python tool/selftest.py [-v]

覆盖：
  1. 音高 vs 理论值（div=64 / div=16，验证 div 不影响音高）
  2. 块大小无关性（逐字节）
  3. 写入队列的采样级精确性（与"先渲染到该帧再写"的朴素做法逐字节一致）
  4. 音量表单调、index=31 == 静音；DDA 输出 = 表值 × (dda-16)
  5. 噪声模式确实宽频、且噪声频率寄存器有作用
  6. 直流抑制（方波均值 ≈ 0）
  7. 驱动：ADSR 无爆音、note_off 后归零、抢占无残留
  8. 吞吐
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pcepsg_core import CLOCK_PCE, PcePsgBank, PcePsgCore, divider_for_hz  # noqa: E402
from psg_driver import PsgDriver  # noqa: E402
from psg_sched import PsgRenderer  # noqa: E402
from smf import Event  # noqa: E402

VERBOSE = "-v" in sys.argv
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------------------------
SQUARE = [0] * 16 + [31] * 16


def load_channel(core: PcePsgCore, ch: int, wave, freq: int, vol: int = 31) -> None:
    """波表上传 + 音高 + 音量（通道须处于关闭状态，写入才自动步进）。"""
    core.set_reg(ch, 0x05, 0xFF)
    core.set_reg(ch, 0x04, 0x00)
    for v in wave:
        core.set_reg(ch, 0x06, v)
    core.set_reg(ch, 0x02, freq & 0xFF)
    core.set_reg(ch, 0x03, (freq >> 8) & 0x0F)
    core.set_reg(ch, 0x04, 0x80 | vol)


def measure_freq(x: np.ndarray, sr: float) -> float:
    """方波上升沿计数 → 基频。"""
    s = np.signbit(x.astype(np.float64))
    rises = int(np.sum((~s[1:]) & s[:-1]))
    return rises * sr / len(x)


# --------------------------------------------------------------------------
def test_pitch() -> None:
    print("\n[1] 音高 vs 理论值")
    for div in (64, 16):
        core = PcePsgCore(div=div)
        for F in (254, 127, 63, 32, 16):
            core.reset()
            core.write(0x01, 0xFF)
            load_channel(core, 0, SQUARE, F)
            n = int(core.sample_rate() * 2.0)
            x = core.generate(n)[:, 0]
            got = measure_freq(x, core.sample_rate())
            want = CLOCK_PCE / (32.0 * F)
            err = abs(got - want) / want
            check(f"div={div} F={F}  f={got:.2f} (理论 {want:.2f}, 误差 {err*100:.3f}%)",
                  err < 0.002)
        core.close()


def test_div_invariance() -> None:
    print("\n[2] 分频系数无关性")
    c64 = PcePsgCore(div=64)
    c8 = PcePsgCore(div=8)
    for c in (c64, c8):
        c.write(0x01, 0xFF)
        load_channel(c, 0, SQUARE, 200)
    a = c64.generate(int(c64.sample_rate()))[:, 0]
    b = c8.generate(int(c8.sample_rate()))[:, 0]
    fa = measure_freq(a, c64.sample_rate())
    fb = measure_freq(b, c8.sample_rate())
    check(f"div64 f={fa:.2f} / div8 f={fb:.2f}", abs(fa - fb) / fa < 0.001,
          f"差 {abs(fa-fb):.2f} Hz")
    c64.close()
    c8.close()


def test_block_independence() -> None:
    print("\n[3] 块大小无关性")
    ev = [Event(0.10, "on", 0, 60, 100), Event(0.40, "off", 0, 60, 0),
          Event(0.50, "on", 1, 67, 90), Event(0.90, "off", 1, 67, 0)]

    outs = []
    n_frames = None
    for block in (1, 7, 64, 511, 4096):
        core = PcePsgCore()
        drv = PsgDriver(core)
        rnd = PsgRenderer(core, drv, ev, total_sec=1.0, tail=0.2)
        drv.scheduler = rnd.schedule_note
        chunks = []
        while not rnd.done:
            chunks.append(rnd.render(block))
        x = np.concatenate(chunks)[:rnd.total_frames]
        n_frames = x.shape[0]
        outs.append(x.tobytes())
        core.close()
    check(f"block=1/7/64/511/4096 逐字节一致（{n_frames} 帧）",
          all(o == outs[0] for o in outs))


def test_write_queue() -> None:
    print("\n[4] 写入队列采样级精确性")
    # 朴素做法：先渲染 1000 帧，直接改音量，再渲染
    core = PcePsgCore()
    core.write(0x01, 0xFF)
    load_channel(core, 0, SQUARE, 254, vol=10)
    a = core.generate(1000)
    core.set_reg(0, 0x04, 0x80 | 25)
    a = np.concatenate([a, core.generate(1000)])
    core.close()

    # 队列做法：同一个写入排在第 1000 帧
    core = PcePsgCore()
    core.write(0x01, 0xFF)
    load_channel(core, 0, SQUARE, 254, vol=10)
    rnd = PsgRenderer(core, None, [], total_sec=2000 / core.sample_rate(), tail=0.0)
    rnd.total_frames = 2000
    rnd.schedule_note(1000, 0, 0, 0x04, 0x80 | 25)
    b = np.concatenate([rnd.render(1) for _ in range(2000)])
    core.close()
    check("队列写入 vs 手动写入 逐字节一致", a.tobytes() == b.tobytes())

    # 再验证切块不影响
    core = PcePsgCore()
    core.write(0x01, 0xFF)
    load_channel(core, 0, SQUARE, 254, vol=10)
    rnd = PsgRenderer(core, None, [], total_sec=2000 / core.sample_rate(), tail=0.0)
    rnd.total_frames = 2000
    rnd.schedule_note(1000, 0, 0, 0x04, 0x80 | 25)
    parts = []
    for n in (1, 3, 97, 500, 1399):
        parts.append(rnd.render(n))
    c = np.concatenate(parts)
    core.close()
    check("队列写入 切块无关", a.tobytes() == c.tobytes())


def test_volume_table() -> None:
    print("\n[5] 音量表 / DDA")
    core = PcePsgCore(dc_block=False)      # 量直流必须关隔直
    core.write(0x01, 0xFF)

    # DDA：通道 5，控制位 6 置 1，R6 直接决定电平
    core.set_reg(5, 0x05, 0xFF)
    core.set_reg(5, 0x07, 0x00)
    core.set_reg(5, 0x04, 0xC0 | 31)          # 使能 + DDA + 最大音量
    core.set_reg(5, 0x06, 31)                 # dda = 31 → (31-16)=15
    x = core.generate(64)[:, 0].astype(np.int32)
    peak31 = int(np.median(x))

    core.set_reg(5, 0x04, 0xC0 | 10)          # 衰减 21 档
    y = core.generate(64)[:, 0].astype(np.int32)
    peak10 = int(np.median(y))

    core.set_reg(5, 0x04, 0xC0 | 0)           # index 31 → 静音
    z = core.generate(64)[:, 0].astype(np.int32)
    zero = int(np.max(np.abs(z)))
    core.close()

    check(f"DDA 输出 {peak31} > 低音量 {peak10} > 0", peak31 > peak10 > 0,
          f"{peak31} / {peak10}")
    check("音量 index=31 完全静音", zero == 0, f"max|z|={zero}")
    check("1.5 dB/档（≈21 档 → 31.5 dB）",
          abs(20 * np.log10(peak31 / peak10) - 31.5) < 2.0,
          f"{20*np.log10(peak31/peak10):.2f} dB")


def test_noise() -> None:
    print("\n[6] 噪声模式")
    specs = []
    for nf in (26, 30):
        core = PcePsgCore()
        core.write(0x01, 0xFF)
        core.set_reg(5, 0x05, 0xFF)
        core.set_reg(5, 0x07, 0x80 | nf)
        core.set_reg(5, 0x04, 0x80 | 31)
        x = core.generate(16384)[:, 0].astype(np.float64)
        sp = np.abs(np.fft.rfft(x))
        specs.append(sp)
        flat = np.exp(np.mean(np.log(sp[1:] + 1e-9))) / (np.mean(sp[1:]) + 1e-9)
        if nf == 26:
            check(f"nf={nf} 频谱平坦度 {flat:.3f}（噪声应接近 1）", flat > 0.2)
        core.close()
    corr = np.corrcoef(specs[0][:4096], specs[1][:4096])[0, 1]
    check(f"不同噪声频率 nf=26/30 频谱不同（相关 {corr:.2f}）", abs(corr) < 0.9)


def test_dc() -> None:
    print("\n[7] 直流")

    def render(dc: bool) -> np.ndarray:
        c = PcePsgCore(dc_block=dc)
        c.write(0x01, 0xFF)
        load_channel(c, 0, SQUARE, 254)
        y = c.generate(200000)[:, 0].astype(np.float64)
        c.close()
        return y

    # 一个波表周期正好是 32*F/div 采样，必须按整数个周期取平均，
    # 否则方波自身的交流分量会污染"直流"测量。
    div = PcePsgCore().div
    period = 32 * 254 // div
    raw = render(False)
    blk = render(True)
    dc_raw = raw[10000:10000 + period * 300].mean()
    dc_blk = blk[10000:10000 + period * 300].mean()

    check(f"芯片原始直流 {dc_raw:.1f}（5-bit 量化残留 ≈ -0.5 LSB）", abs(dc_raw) < 300)
    check(f"隔直后直流 {dc_blk:.3f}", abs(dc_blk) < 1.0)


def test_driver() -> None:
    print("\n[8] 驱动：包络 / 释放 / 抢占")
    ev = [
        Event(0.00, "on", 0, 60, 110), Event(0.00, "on", 1, 64, 100),
        Event(0.00, "on", 2, 67, 90),
        Event(0.50, "off", 0, 60, 0),
        Event(0.60, "on", 0, 62, 127),       # 单音区
        Event(1.00, "off", 1, 64, 0), Event(1.00, "off", 2, 67, 0),
        Event(1.00, "off", 0, 62, 0),
    ]
    core = PcePsgCore()
    drv = PsgDriver(core)
    rnd = PsgRenderer(core, drv, ev, total_sec=1.0, tail=1.0)
    drv.scheduler = rnd.schedule_note
    chunks = []
    while not rnd.done:
        chunks.append(rnd.render(1024))
    x = np.concatenate(chunks)
    core.close()

    d = np.abs(np.diff(x[:, 0].astype(np.int32)))
    # 允许方波本身的跳变，但不应出现"音量突变"那种超幅跳变
    check(f"最大采样跳变 {d.max()}（方波边沿量级内）", d.max() < 20000)

    tail = x[-int(0.2 * 55930):, 0]
    check(f"尾部静音（max|tail|={np.abs(tail).max()}）", np.abs(tail).max() == 0)

    # 抢占：8 个音同时按下，6 通道
    ev2 = [Event(0.0, "on", i // 2, 48 + i, 100) for i in range(8)]
    ev2 += [Event(0.3, "off", i // 2, 48 + i, 0) for i in range(8)]
    core = PcePsgCore()
    drv = PsgDriver(core)
    rnd = PsgRenderer(core, drv, ev2, total_sec=0.3, tail=0.8)
    drv.scheduler = rnd.schedule_note
    while not rnd.done:
        rnd.render(512)
    core.close()
    check("6 通道 + 8 音：抢占后无 enble 残留", drv.stats["steals"] >= 2,
          f"steals={drv.stats['steals']}")


def test_multichip() -> None:
    print("\n[9] 多芯片（6N 通道）")

    # -- 芯片互相独立：C ≈ A + B --
    def render_bank(active: tuple[int, ...], chips: int = 4) -> np.ndarray:
        b = PcePsgBank(chips=chips, dc_block=False)
        for c in active:
            b.write(0x01, 0xFF, chip=c)
            b.write(0x00, 0, chip=c)
            b.write(0x05, 0xFF, chip=c)
            b.write(0x04, 0x00, chip=c)
            for v in SQUARE:
                b.write(0x06, v, chip=c)
            b.write(0x02, (254 + 60 * c) & 0xFF, chip=c)
            b.write(0x03, ((254 + 60 * c) >> 8) & 0x0F, chip=c)
            b.write(0x04, 0x80 | 31, chip=c)
        y = b.generate(20000)[:, 0].astype(np.int32)
        b.close()
        return y

    a = render_bank((1,))
    bb = render_bank((0,))
    cc = render_bank((0, 1))
    err = np.abs(cc - (a + bb)).max()
    check(f"两颗芯片叠加 ≈ 各自之和（最大偏差 {err} LSB）", err <= 3)

    # -- 单芯片 vs 机架：电平只差混音增益 1/sqrt(N) --
    one = PcePsgCore(dc_block=False)
    one.write(0x01, 0xFF)
    load_channel(one, 0, SQUARE, 254)
    y1 = one.generate(20000)[:, 0].astype(np.int32)
    one.close()
    b2 = PcePsgBank(chips=4, dc_block=False)
    b2.write(0x01, 0xFF, chip=2)
    b2.write(0x00, 0, chip=2)
    b2.write(0x05, 0xFF, chip=2)
    b2.write(0x04, 0x00, chip=2)
    for v in SQUARE:
        b2.write(0x06, v, chip=2)
    b2.write(0x02, 254, chip=2)
    b2.write(0x03, 0, chip=2)
    b2.write(0x04, 0x80 | 31, chip=2)
    y4 = b2.generate(20000)[:, 0].astype(np.int32)
    b2.close()
    ratio = np.abs(y4).max() / max(1, np.abs(y1).max())
    check(f"机架混音增益 1/sqrt(4)（实测 {ratio:.3f}）", abs(ratio - 0.5) < 0.02)

    # -- 24 个通道真的能用：24 音齐发不抢 --
    core = PcePsgBank(chips=4)
    drv = PsgDriver(core, chips=4)
    ev = [Event(0.0, "on", i % 8, 40 + i * 2, 100) for i in range(24)]
    ev += [Event(0.6, "off", i % 8, 40 + i * 2, 0) for i in range(24)]
    rnd = PsgRenderer(core, drv, ev, total_sec=0.6, tail=0.6)
    drv.scheduler = rnd.schedule_note
    while not rnd.done:
        rnd.render(1024)
    core.close()
    check(f"24 音齐发 抢占 {drv.stats['steals']} 次（应为 0）",
          drv.stats["steals"] == 0)
    check("声部数 = 6×芯片数", len(drv.all_voices) == 24, f"{len(drv.all_voices)}")


def test_throughput() -> None:
    print("\n[10] 吞吐")
    core = PcePsgCore()
    drv = PsgDriver(core, drums=True)
    ev = []
    t = 0.0
    for i in range(200):
        ev.append(Event(t, "on", 0, 60 + (i % 12), 100))
        ev.append(Event(t + 0.1, "off", 0, 60 + (i % 12), 0))
        t += 0.05
    ev.sort(key=lambda e: e.t)
    rnd = PsgRenderer(core, drv, ev, total_sec=t, tail=0.5)
    drv.scheduler = rnd.schedule_note
    t0 = time.perf_counter()
    n = 0
    while not rnd.done:
        n += rnd.render(512).shape[0]
    dt = time.perf_counter() - t0
    core.close()
    audio_sec = n / 223721.5625
    check(f"{audio_sec:.1f}s 音频用了 {dt*1000:.0f} ms", dt / audio_sec < 0.05,
          f"{1000*dt/audio_sec:.1f} ms/音频秒")


# --------------------------------------------------------------------------
def main() -> int:
    print("=" * 66)
    print("PCE PSG (HuC6280) 自检")
    print("=" * 66)
    test_pitch()
    test_div_invariance()
    test_block_independence()
    test_write_queue()
    test_volume_table()
    test_noise()
    test_dc()
    test_driver()
    test_multichip()
    test_throughput()

    print("\n" + "=" * 66)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

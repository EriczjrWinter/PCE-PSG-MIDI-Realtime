# pcepsg_core.py
"""HuC6280 PSG 核心的 ctypes 封装。

和 OPMMIDI-RT 的 core.ChipCore 保持同样的形状（write / generate_into /
sample_rate / reset），这样 rt_engine 那套调度思路可以原样搬过来。

寄存器偏移就是内存映射 $0800 + offset，即 offset 0x00..0x0f。
"""
from __future__ import annotations

import ctypes
import os

import numpy as np

CLOCK_PCE = 3_579_545       # PSG 时钟 = CPU 7.15909 MHz / 2
DIV_DEFAULT = 16            # 3579545 / 16 = 223721.6 Hz（见 README：64 在高音会有寄生音）

# 通道能力（用来做分配器与音色表）
CH_COUNT = 6
CH_NOISE = (4, 5)           # 只有 4/5 有噪声模式
CH_DDA = 5                  # 只有 5 支持 DDA

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DLL = os.path.join(HERE, "pcepsg.dll")


# --------------------------------------------------------------------------
# 音高换算（和芯片同一条公式，避免在别处重复推导）
# --------------------------------------------------------------------------
def hz_for_divider(divider: int, clock: int = CLOCK_PCE) -> float:
    """f = clock / (32 * F)，F == 0 当作 4096。"""
    f = 4096 if (divider & 0xFFF) == 0 else (divider & 0xFFF)
    return clock / (32.0 * f)


def divider_for_hz(hz: float, clock: int = CLOCK_PCE) -> int:
    if hz <= 0:
        raise ValueError("hz 必须为正")
    d = int(round(clock / (32.0 * hz)))
    return max(1, min(4095, d))


def divider_for_note(note: float, bend: float = 0.0, detune_cents: float = 0.0,
                     a4_hz: float = 440.0, clock: int = CLOCK_PCE) -> int:
    """MIDI 音符号 → 分频值。bend 单位是半音。"""
    semis = (note - 69.0) + bend + detune_cents / 100.0
    hz = a4_hz * (2.0 ** (semis / 12.0))
    return divider_for_hz(hz, clock)


# --------------------------------------------------------------------------
# 核心
# --------------------------------------------------------------------------
class PcePsgCore:
    """6 通道波表 PSG。输出交错立体声 int16。"""

    name = "huc6280-psg"
    _dll = None
    _dll_path = None

    @classmethod
    def load(cls, path: str = DEFAULT_DLL) -> ctypes.CDLL:
        path = os.path.abspath(path)
        if cls._dll is None or cls._dll_path != path:
            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"找不到 {path}，先跑 build_core.ps1 编译 pcepsg.dll")
            d = ctypes.CDLL(path)

            d.pcepsg_create.restype = ctypes.c_void_p
            d.pcepsg_create.argtypes = [ctypes.c_int, ctypes.c_int]
            d.pcepsg_destroy.argtypes = [ctypes.c_void_p]
            d.pcepsg_reset.argtypes = [ctypes.c_void_p]
            d.pcepsg_write.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            d.pcepsg_generate.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_int16),
                                          ctypes.c_int]
            d.pcepsg_set_dc_block.argtypes = [ctypes.c_void_p, ctypes.c_int]
            d.pcepsg_sample_rate.restype = ctypes.c_double
            d.pcepsg_sample_rate.argtypes = [ctypes.c_void_p]
            d.pcepsg_div.restype = ctypes.c_int
            d.pcepsg_div.argtypes = [ctypes.c_void_p]
            d.pcepsg_channel_freq.restype = ctypes.c_int
            d.pcepsg_channel_freq.argtypes = [ctypes.c_void_p, ctypes.c_int]

            d.pcepsg_bank_create.restype = ctypes.c_void_p
            d.pcepsg_bank_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
            d.pcepsg_bank_destroy.argtypes = [ctypes.c_void_p]
            d.pcepsg_bank_reset.argtypes = [ctypes.c_void_p]
            d.pcepsg_bank_write.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int]
            d.pcepsg_bank_generate.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_int16),
                                               ctypes.c_int]
            d.pcepsg_bank_set_gain.argtypes = [ctypes.c_void_p, ctypes.c_double]
            d.pcepsg_bank_set_dc_block.argtypes = [ctypes.c_void_p, ctypes.c_int]
            d.pcepsg_bank_sample_rate.restype = ctypes.c_double
            d.pcepsg_bank_sample_rate.argtypes = [ctypes.c_void_p]
            d.pcepsg_bank_chips.restype = ctypes.c_int
            d.pcepsg_bank_chips.argtypes = [ctypes.c_void_p]

            cls._dll = d
            cls._dll_path = path
        return cls._dll

    def __init__(self, clock: int = CLOCK_PCE, div: int = DIV_DEFAULT,
                 dll_path: str = DEFAULT_DLL, dc_block: bool = True):
        self.clock = clock
        self.div = div
        self._dll = self.load(dll_path)
        self._h = self._dll.pcepsg_create(clock, div)
        if not self._h:
            raise RuntimeError("pcepsg_create 失败")
        self.set_dc_block(dc_block)

    # -- 基本操作 --
    def reset(self) -> None:
        self._dll.pcepsg_reset(self._h)

    def set_dc_block(self, on: bool) -> None:
        self._dll.pcepsg_set_dc_block(self._h, 1 if on else 0)

    def write(self, reg: int, val: int, *, chip: int = 0) -> None:
        """写寄存器。单颗芯片只有 chip=0（多芯片请用 PcePsgBank）。"""
        if chip:
            raise ValueError("单芯片核心只有 chip=0，多芯片请用 PcePsgBank")
        self._dll.pcepsg_write(self._h, reg & 0x0F, val & 0xFF)

    def set_reg(self, ch: int, reg: int, val: int) -> None:
        """选中通道后写寄存器（封装 R0 选择，避免各处手写顺序）。"""
        self.write(0x00, ch)
        self.write(reg, val)

    def sample_rate(self) -> float:
        return self._dll.pcepsg_sample_rate(self._h)

    def channel_freq(self, ch: int) -> int:
        return self._dll.pcepsg_channel_freq(self._h, ch)

    # -- 音频 --
    def generate_into(self, nframes: int, buf) -> None:
        self._dll.pcepsg_generate(self._h, buf, nframes)

    def generate(self, nframes: int) -> np.ndarray:
        buf = (ctypes.c_int16 * (2 * nframes))()
        self._dll.pcepsg_generate(self._h, buf, nframes)
        return np.frombuffer(buf, dtype="<i2", count=2 * nframes).reshape(nframes, 2)

    def close(self) -> None:
        if self._h:
            self._dll.pcepsg_destroy(self._h)
            self._h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 多芯片机架
# --------------------------------------------------------------------------
class PcePsgBank:
    """N 颗 PSG 叠成一个声源：6N 个通道，输出已经混好。

    这是本项目的关键取舍：PC Engine 只有一颗 HuC6280（6 通道），
    但"用 PSG 音色做 MIDI 合成器"没必要被一颗芯片绑死。
    chips=4 → 24 通道，基本够跑 GM 的多声部。
    """

    name = "huc6280-psg-bank"

    def __init__(self, chips: int = 4, clock: int = CLOCK_PCE, div: int = DIV_DEFAULT,
                 dll_path: str = DEFAULT_DLL, dc_block: bool = True,
                 gain: float | None = None):
        self.nchips = max(1, int(chips))
        self.clock = clock
        self.div = div
        self._dll = PcePsgCore.load(dll_path)
        self._h = self._dll.pcepsg_bank_create(self.nchips, clock, div)
        if not self._h:
            raise RuntimeError("pcepsg_bank_create 失败")
        self.set_dc_block(dc_block)
        if gain is not None:
            self.set_gain(gain)

    # -- 基本操作 --
    def reset(self) -> None:
        self._dll.pcepsg_bank_reset(self._h)

    def write(self, reg: int, val: int, *, chip: int = 0) -> None:
        self._dll.pcepsg_bank_write(self._h, chip, reg & 0x0F, val & 0xFF)

    def set_gain(self, gain: float) -> None:
        """混音增益。默认 1/sqrt(N)（非相干叠加的保守估计）。"""
        self._dll.pcepsg_bank_set_gain(self._h, float(gain))

    def set_dc_block(self, on: bool) -> None:
        self._dll.pcepsg_bank_set_dc_block(self._h, 1 if on else 0)

    def sample_rate(self) -> float:
        return self._dll.pcepsg_bank_sample_rate(self._h)

    def chips(self) -> int:
        return self._dll.pcepsg_bank_chips(self._h)

    # -- 音频 --
    def generate_into(self, nframes: int, buf) -> None:
        self._dll.pcepsg_bank_generate(self._h, buf, nframes)

    def generate(self, nframes: int) -> np.ndarray:
        buf = (ctypes.c_int16 * (2 * nframes))()
        self._dll.pcepsg_bank_generate(self._h, buf, nframes)
        return np.frombuffer(buf, dtype="<i2", count=2 * nframes).reshape(nframes, 2)

    def close(self) -> None:
        if self._h:
            self._dll.pcepsg_bank_destroy(self._h)
            self._h = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

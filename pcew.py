# pcew.py
""".pcew 波表音色库（v1 用 Python dict 表示，二进制格式留到需要时再定）。

32 步 × 5 bit 的波表决定了音色上限：它必然混叠，也必然"锯齿感"。
这里不去做带限，只在选波形时回避最刺耳的组合 —— 那是 PCE 的本音。

乐器按 GM 的 8 个 program 一组映射到 16 个"乐器组"，而不是硬套 128 个音色：
6 个通道本来就装不下 GM。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

STEPS = 32
MAXV = 31


def _q(x: float) -> int:
    return max(0, min(MAXV, int(round(x))))


def _sine(phase: float = 0.0) -> list[int]:
    return [_q(15.5 + 15.5 * math.sin(2 * math.pi * (i / STEPS) + phase)) for i in range(STEPS)]


def _harm(*amps: tuple[float, float]) -> list[int]:
    """amps = [(倍频, 幅度), ...]，幅度总和归一到 15.5。"""
    out = []
    for i in range(STEPS):
        t = 2 * math.pi * i / STEPS
        s = sum(a * math.sin(k * t) for k, a in amps)
        out.append(_q(15.5 + s))
    return out


def _square(duty: float) -> list[int]:
    n = max(1, min(STEPS - 1, int(round(STEPS * duty))))
    return [MAXV] * n + [0] * (STEPS - n)


WAVES: dict[str, list[int]] = {
    # 方波家族：最亮、最"PCE"
    "square":   _square(0.5),
    "pulse25":  _square(0.25),
    "pulse12":  _square(0.125),
    # 三角/正弦：高频时最不容易糊，适合铺底和长音
    "triangle": [_q(15.5 + 15.5 * (1 - 4 * abs(i / STEPS - 0.5))) for i in range(STEPS)],
    "sine":     _sine(),
    # 锯齿：弦乐/铜管的粗糙感
    "saw":      [_q(15.5 + 15.5 * (2.0 * i / STEPS - 1.0)) for i in range(STEPS)],
    # 混合波：给贝斯/风琴一点个性
    "bass":     _harm((1, 14.0), (2, 5.0), (3, 2.0)),
    "organ":    _harm((1, 11.0), (2, 8.0), (3, 6.0), (4, 4.0)),
    "lead":     _harm((1, 12.0), (2, 4.0), (3, 6.0), (5, 2.0)),
    "bell":     _harm((1, 6.0), (3, 7.0), (5, 5.0), (7, 4.0)),
    "hollow":   _harm((1, 9.0), (3, 9.0), (5, 5.0)),
}


@dataclass(frozen=True)
class Instrument:
    name: str
    wave: str
    attack: float = 2.0      # ms
    decay: float = 200.0     # ms
    sustain: float = 0.4     # 0..1（线性幅度，内部再转 dB）
    release: float = 120.0   # ms
    level: float = 1.0       # 0..1，整体音量缩放
    detune: float = 0.0      # cents

    def as_dict(self) -> dict:
        return {
            "name": self.name, "wave": self.wave,
            "attack": self.attack, "decay": self.decay,
            "sustain": self.sustain, "release": self.release,
            "level": self.level, "detune": self.detune,
        }


# 16 个乐器组，顺序与 GM 的 program//8 对齐
INSTRUMENTS: list[Instrument] = [
    Instrument("piano",    "square",  attack=1.0,  decay=420.0, sustain=0.18, release=110.0, level=0.95),
    Instrument("chromatic", "triangle", attack=1.0, decay=380.0, sustain=0.22, release=120.0, level=0.95),
    Instrument("organ",    "organ",   attack=4.0,  decay=0.0,   sustain=1.00, release=45.0,  level=0.75),
    Instrument("guitar",   "saw",     attack=1.0,  decay=300.0, sustain=0.25, release=110.0, level=0.85),
    Instrument("bass",     "bass",    attack=2.0,  decay=260.0, sustain=0.42, release=70.0,  level=0.95),
    Instrument("strings",  "saw",     attack=140.0, decay=0.0,  sustain=0.85, release=280.0, level=0.70),
    Instrument("ensemble", "organ",   attack=90.0, decay=0.0,   sustain=0.85, release=300.0, level=0.65),
    Instrument("brass",    "lead",    attack=32.0, decay=120.0, sustain=0.62, release=110.0, level=0.80),
    Instrument("reed",     "pulse25", attack=26.0, decay=140.0, sustain=0.66, release=90.0,  level=0.78),
    Instrument("pipe",     "sine",    attack=22.0, decay=60.0,  sustain=0.88, release=70.0,  level=0.85),
    Instrument("lead",     "square",  attack=2.0,  decay=80.0,  sustain=0.72, release=70.0,  level=0.80),
    Instrument("pad",      "triangle", attack=220.0, decay=0.0, sustain=0.95, release=420.0, level=0.68),
    Instrument("fx",       "hollow",  attack=8.0,  decay=600.0, sustain=0.30, release=400.0, level=0.70),
    Instrument("ethnic",   "pulse12", attack=6.0,  decay=260.0, sustain=0.35, release=130.0, level=0.80),
    Instrument("perc",     "bell",    attack=0.0,  decay=120.0, sustain=0.0,  release=120.0, level=0.85),
    Instrument("sfx",      "hollow",  attack=0.0,  decay=90.0,  sustain=0.0,  release=90.0,  level=0.80),
]

DRUM = Instrument("drum", "bass", attack=0.0, decay=200.0, sustain=0.0, release=200.0, level=1.0)


# --------------------------------------------------------------------------
# 音色风格：不换 ADSR，只换波形，用来快速听"这套引擎的三种脸"
# --------------------------------------------------------------------------
STYLES = ("standard", "soft", "bright")

_STYLE_WAVES: dict[str, dict[str, str]] = {
    "standard": {},
    "soft": {
        "piano": "triangle", "chromatic": "sine", "organ": "organ",
        "guitar": "triangle", "bass": "sine", "strings": "triangle",
        "ensemble": "organ", "brass": "lead", "reed": "sine",
        "pipe": "sine", "lead": "triangle", "pad": "sine",
        "fx": "hollow", "ethnic": "sine", "perc": "bell", "sfx": "hollow",
    },
    "bright": {
        "piano": "pulse25", "chromatic": "square", "organ": "square",
        "guitar": "pulse25", "bass": "saw", "strings": "saw",
        "ensemble": "square", "brass": "square", "reed": "pulse12",
        "pipe": "pulse25", "lead": "square", "pad": "pulse25",
        "fx": "pulse12", "ethnic": "pulse12", "perc": "bell", "sfx": "hollow",
    },
}

_style = "standard"


def set_style(name: str) -> None:
    global _style
    _style = name if name in _STYLE_WAVES else "standard"


def get_style() -> str:
    return _style


def instrument_for_program(prog: int) -> Instrument:
    """GM program → 乐器组。鼓组(128)在调用方就被路由掉了。"""
    if prog < 0 or prog > 127:
        prog = 0
    inst = INSTRUMENTS[min(len(INSTRUMENTS) - 1, prog // 8)]
    wave = _STYLE_WAVES.get(_style, {}).get(inst.name)
    if wave is None or wave == inst.wave:
        return inst
    return replace(inst, wave=wave)


def wave_bytes(name: str) -> bytes:
    return bytes(WAVES[name])

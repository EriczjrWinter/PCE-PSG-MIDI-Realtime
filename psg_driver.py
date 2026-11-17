# psg_driver.py
"""多芯片 6N 通道声部管理 + 软件 ADSR + 噪声/扫频鼓。

PCE 历史上只有一颗 HuC6280（6 通道），但"用 PSG 音色做 MIDI 合成器"不必
被一颗芯片绑死：`chips=4` 就是 24 个通道，相当于四台 PCE 一起响。
每颗芯片内部仍然是严格的 6 通道行为，这里只负责跨芯片分配声部。

调度模型
--------
每个声部自己维护 `next_frame`（下一次音量/音高变化发生在第几帧）。
Renderer 用它和 MIDI 事件、WriteQueue 一起决定切块边界，所以包络是
**采样级精确**的，而不是按 60 Hz 帧率糊上去的。

一个声部一次只有一个"下一次变化"，因此这里是 6N 项的最小堆 ——
比通用 WriteQueue 快一个数量级。通用队列留给"一次性延迟写入"（鼓的扫频等）。
"""
from __future__ import annotations

from pcepsg_core import CLOCK_PCE, divider_for_note
from pcew import DRUM, INSTRUMENTS, Instrument, WAVES, instrument_for_program

INF = 1 << 62

# 声部相位
OFF, ATTACK, DECAY, SUSTAIN, RELEASE = 0, 1, 2, 3, 4

# 衰减指数：0 = 最大声，31 = 静音（volume_table[31] == 0）
SILENT = 31
DB_PER_STEP = 1.5

# 力度映射的动态范围（18 档 ≈ 27 dB）
VEL_RANGE = 18


def _clamp(x: int, lo: int = 0, hi: int = 31) -> int:
    return lo if x < lo else (hi if x > hi else x)


class Voice:
    """一个 (芯片, 通道) 时隙。"""

    __slots__ = ("chip", "ch", "active", "phase", "mch", "note", "vel", "inst",
                 "bend", "noise", "nf", "drum", "level", "peak", "sus",
                 "t0", "dt", "k", "steps", "lvl0", "target", "next_frame",
                 "step_sign", "a_frames", "d_frames", "r_frames", "seq", "pan")

    def __init__(self, chip: int, ch: int):
        self.chip = chip
        self.ch = ch
        self.reset()

    def reset(self) -> None:
        self.active = False
        self.phase = OFF
        self.mch = -1
        self.note = -1
        self.vel = 0
        self.inst = None
        self.bend = 0.0
        self.noise = False
        self.nf = 0
        self.drum = False
        self.level = SILENT
        self.peak = SILENT
        self.sus = SILENT
        self.t0 = 0.0
        self.dt = 0.0
        self.k = 0
        self.steps = 0
        self.lvl0 = SILENT
        self.target = SILENT
        self.next_frame = INF
        self.step_sign = 1
        self.a_frames = 0.0
        self.d_frames = 0.0
        self.r_frames = 0.0
        self.seq = 0
        self.pan = 64

    def __repr__(self) -> str:
        return f"<Voice chip={self.chip} ch={self.ch} mch={self.mch} note={self.note}>"

    @property
    def key(self) -> tuple[int, int]:
        return (self.chip, self.ch)


class PsgDriver:
    """把 MIDI 事件翻译成 PSG 寄存器写入（可跨多颗芯片）。"""

    def __init__(self, core, *, drums: bool = False, chips: int = 1,
                 clock: int = CLOCK_PCE):
        self.core = core
        self.clock = clock
        self.chips = max(1, chips)
        self.sr = core.sample_rate()
        self.drums = drums

        self.all_voices = [Voice(c, ch) for c in range(self.chips) for ch in range(6)]
        # 鼓占芯片 0 的通道 4/5，其余全部给旋律
        self.drum_kick = (0, 4)
        self.drum_noise = (0, 5)
        self.melodic_voices = [v for v in self.all_voices
                               if not (drums and v.chip == 0 and v.ch >= 4)]

        self._loaded: dict[tuple[int, int], str | None] = {v.key: None for v in self.all_voices}
        self._sounding: dict[tuple[int, int], Voice] = {}
        self._seq = 0

        self.mch_inst: dict[int, Instrument] = {c: INSTRUMENTS[0] for c in range(16)}
        self.mch_vol: dict[int, float] = {c: 1.0 for c in range(16)}
        self.mch_pan: dict[int, int] = {c: 64 for c in range(16)}
        self.bend_range = 2.0            # 半音

        # 可选：一次性延迟写入的入口（由 Renderer 注入），签名 (frame, chip, ch, reg, val)
        self.scheduler = None

        self.stats = {"notes": 0, "steals": 0, "writes": 0, "chips": self.chips}

        for c in range(self.chips):
            self.core.write(0x01, 0xFF, chip=c)      # 总音量 L/R 拉满
            for ch in range(6):
                self.core.write(0x00, ch, chip=c)
                self.core.write(0x05, 0xFF, chip=c)  # 平衡居中（= 不衰减）
                self.core.write(0x04, 0x00, chip=c)  # 全关
                self.core.write(0x07, 0x00, chip=c)  # 噪声关

    # ------------------------------------------------------------------
    # 基本写入
    # ------------------------------------------------------------------
    def _ms_to_frames(self, ms: float) -> float:
        return max(0.0, ms) * self.sr / 1000.0

    def _write(self, chip: int, ch: int, reg: int, val: int) -> None:
        self.core.write(0x00, ch, chip=chip)
        self.core.write(reg, val, chip=chip)
        self.stats["writes"] += 1

    def _write_level(self, v: Voice) -> None:
        al = SILENT - v.level          # index 31 → al 0（静音）
        self._write(v.chip, v.ch, 0x04, 0x80 | al)

    def _write_balance(self, chip: int, ch: int, pan: int) -> None:
        """pan 0..127 → (L<<4)|R；64 居中 = 两侧都不衰减（硬件语义）。"""
        right_att = max(0, pan - 64)
        left_att = max(0, 64 - pan)
        l = 15 - int(round(left_att / 64.0 * 15))
        r = 15 - int(round(right_att / 64.0 * 15))
        self._write(chip, ch, 0x05, (l << 4) | r)

    def _upload_wave(self, chip: int, ch: int, name: str) -> None:
        """波表上传只在音色变化时做；必须在通道关闭时写才会自动步进。"""
        key = (chip, ch)
        if self._loaded.get(key) == name:
            return
        wave = WAVES[name]
        self._write(chip, ch, 0x04, 0x00)
        self.core.write(0x00, ch, chip=chip)
        for v in wave:
            self.core.write(0x06, v, chip=chip)
        self.stats["writes"] += 1 + len(wave)
        self._loaded[key] = name

    # ------------------------------------------------------------------
    # 声部分配
    # ------------------------------------------------------------------
    def _free_voice(self) -> Voice | None:
        for v in self.melodic_voices:
            if not v.active:
                return v
        return None

    def _steal(self) -> Voice:
        def key(v: Voice):
            # 先抢释放中的，再抢最安静的，最后抢最老的
            return (0 if v.phase == RELEASE else 1, -v.level, v.seq)

        return min(self.melodic_voices, key=key)

    def _kill(self, v: Voice) -> None:
        if v.mch >= 0:
            self._sounding.pop((v.mch, v.note), None)
        self._write(v.chip, v.ch, 0x04, 0x00)
        v.reset()

    # ------------------------------------------------------------------
    # 包络
    # ------------------------------------------------------------------
    def _begin_phase(self, v: Voice, phase: int, frame: int) -> None:
        if not v.active:
            return
        if phase == ATTACK:
            lvl0, target, dur = SILENT, v.peak, v.a_frames
        elif phase == DECAY:
            lvl0, target, dur = v.peak, v.sus, v.d_frames
        elif phase == RELEASE:
            lvl0, target, dur = v.level, SILENT, v.r_frames
        else:                                   # SUSTAIN
            v.phase = SUSTAIN
            v.next_frame = INF
            return

        v.phase = phase
        v.lvl0 = int(lvl0)
        v.target = int(target)
        steps = abs(v.target - v.lvl0)

        # 时长为 0 或没有级差：立即到位
        if steps == 0 or dur <= 0.0:
            v.k = 0
            v.steps = 0
            v.level = v.target
            self._write_level(v)
            self._on_phase_end(v, frame)
            return

        v.steps = steps
        v.step_sign = 1 if v.target > v.lvl0 else -1
        v.dt = dur / steps
        v.t0 = float(frame)
        v.k = 0
        v.next_frame = frame + max(1, int(round(v.dt)))

    def _on_phase_end(self, v: Voice, frame: int) -> None:
        if not v.active:
            return
        if v.phase == ATTACK:
            self._begin_phase(v, DECAY if v.d_frames > 0 else SUSTAIN, frame)
        elif v.phase == DECAY:
            if v.level >= SILENT:
                self._kill(v)
            else:
                v.phase = SUSTAIN
                v.next_frame = INF
        elif v.phase == RELEASE:
            self._kill(v)
        else:
            v.next_frame = INF

    def _advance_voice(self, v: Voice, frame: int) -> None:
        guard = 0
        while v.active and v.next_frame <= frame:
            v.k += 1
            v.level = _clamp(v.lvl0 + v.step_sign * v.k)
            self._write_level(v)
            if v.k >= v.steps:
                self._on_phase_end(v, frame)
                return
            v.next_frame = int(round(v.t0 + v.dt * (v.k + 1)))
            if v.next_frame <= frame:
                guard += 1
                if guard > 4096:
                    v.next_frame = frame + 1
                    return

    # ------------------------------------------------------------------
    # MIDI 事件
    # ------------------------------------------------------------------
    def program_change(self, frame: int, mch: int, prog: int) -> None:
        self.mch_inst[mch] = instrument_for_program(prog)

    def pitch_bend(self, frame: int, mch: int, lsb: int, msb: int) -> None:
        semis = (((msb << 7) | lsb) - 8192) / 8192.0 * self.bend_range
        for v in self.all_voices:
            if v.active and v.mch == mch and not v.noise and not v.drum:
                v.bend = semis
                detune = v.inst.detune if v.inst else 0.0
                div = divider_for_note(v.note, semis, detune, clock=self.clock)
                self._write(v.chip, v.ch, 0x02, div & 0xFF)
                self._write(v.chip, v.ch, 0x03, (div >> 8) & 0x0F)

    def control_change(self, frame: int, mch: int, cc: int, val: int) -> None:
        if cc == 7:
            self.mch_vol[mch] = val / 127.0
            self._refresh_levels(mch)
        elif cc == 10:
            self.mch_pan[mch] = val
            for v in self.all_voices:
                if v.active and v.mch == mch:
                    v.pan = val
                    self._write_balance(v.chip, v.ch, val)
        elif cc == 123:                          # All Notes Off
            for v in list(self.all_voices):
                if v.active and v.mch == mch and v.phase != RELEASE:
                    self._begin_phase(v, RELEASE, frame)

    def _refresh_levels(self, mch: int) -> None:
        """通道音量变化：重算 peak/sus，正在 sustain 的声部立刻改音量。"""
        for v in self.all_voices:
            if not (v.active and v.mch == mch) or v.inst is None:
                continue
            v.peak = self._peak_index(v.vel, v.inst.level, self.mch_vol[mch])
            v.sus = _clamp(v.peak + int(round((SILENT - v.peak) * (1.0 - v.inst.sustain))))
            if v.phase == SUSTAIN:
                v.level = v.sus
                self._write_level(v)

    def _peak_index(self, vel: int, level: float, vol_scale: float) -> int:
        idx = (1.0 - vel / 127.0) * VEL_RANGE
        idx += (1.0 - level) * 16.0
        idx += (1.0 - vol_scale) * 16.0
        return _clamp(int(round(idx)))

    def note_on(self, frame: int, mch: int, note: int, vel: int) -> None:
        if vel <= 0:
            self.note_off(frame, mch, note)
            return

        if mch == 9 and self.drums:
            self._drum_on(frame, note, vel)
            return

        inst = self.mch_inst[mch]

        v = None
        for cand in self.melodic_voices:
            if cand.active and cand.mch == mch and cand.note == note:
                v = cand
                break
        if v is None:
            v = self._free_voice()
        if v is None:
            v = self._steal()
            self.stats["steals"] += 1

        if v.active:
            self._kill(v)

        self._seq += 1
        v.seq = self._seq
        v.active = True
        v.mch = mch
        v.note = note
        v.vel = vel
        v.inst = inst
        v.noise = False
        v.drum = False
        v.bend = 0.0
        v.pan = self.mch_pan[mch]
        v.peak = self._peak_index(vel, inst.level, self.mch_vol[mch])
        v.sus = _clamp(v.peak + int(round((SILENT - v.peak) * (1.0 - inst.sustain))))
        v.a_frames = self._ms_to_frames(inst.attack)
        v.d_frames = self._ms_to_frames(inst.decay)
        v.r_frames = self._ms_to_frames(inst.release)
        v.level = SILENT

        self._upload_wave(v.chip, v.ch, inst.wave)
        div = divider_for_note(note, 0.0, inst.detune, clock=self.clock)
        self._write(v.chip, v.ch, 0x02, div & 0xFF)
        self._write(v.chip, v.ch, 0x03, (div >> 8) & 0x0F)
        self._write_balance(v.chip, v.ch, v.pan)
        self._write(v.chip, v.ch, 0x07, 0x00)                 # 确保不是噪声模式
        self._write(v.chip, v.ch, 0x04, 0x80)                 # key-on（相位复位）+ 静音

        self._sounding[(mch, note)] = v
        self.stats["notes"] += 1
        self._begin_phase(v, ATTACK, frame)

    def note_off(self, frame: int, mch: int, note: int) -> None:
        v = self._sounding.pop((mch, note), None)
        if v is None or not v.active:
            return
        if v.drum or v.noise:
            return
        self._begin_phase(v, RELEASE, frame)

    # ------------------------------------------------------------------
    # 打击乐：底鼓 = 扫频波形，军鼓/镲 = 噪声（只有 4/5 有噪声）
    # ------------------------------------------------------------------
    def _drum_on(self, frame: int, note: int, vel: int) -> None:
        if note in (35, 36):
            self._kick(frame, vel)
        elif note in (38, 40):
            self._noise_hit(frame, vel, nf=27, decay=110.0, peak_off=1)
        elif note in (42, 44, 46):
            self._noise_hit(frame, vel, nf=29, decay=42.0, peak_off=5)
        elif note in (49, 51, 57):
            self._noise_hit(frame, vel, nf=26, decay=900.0, peak_off=3)
        elif note in (37, 39):
            self._noise_hit(frame, vel, nf=28, decay=90.0, peak_off=3)
        else:
            self._noise_hit(frame, vel, nf=28, decay=120.0, peak_off=4)

    def _start_perc_voice(self, chip: int, ch: int, vel: int,
                          decay_ms: float, peak_off: int) -> Voice:
        v = self.all_voices[chip * 6 + ch]
        if v.active:
            self._kill(v)
        self._seq += 1
        v.seq = self._seq
        v.active = True
        v.drum = True
        v.noise = (ch == 5)
        v.mch = -1
        v.note = -1
        v.vel = vel
        v.inst = DRUM
        v.peak = _clamp(self._peak_index(vel, 1.0, 1.0) + peak_off)
        v.sus = SILENT
        v.a_frames = 0.0
        v.d_frames = self._ms_to_frames(decay_ms)
        v.r_frames = v.d_frames
        v.level = SILENT
        return v

    def _kick(self, frame: int, vel: int) -> None:
        chip, ch = self.drum_kick
        self._upload_wave(chip, ch, "bass")
        self._write_balance(chip, ch, 64)
        self._write(chip, ch, 0x07, 0x00)

        # 音高包络 200 Hz → 55 Hz：8 段，走 WriteQueue 采样级落下
        f0, f1, nsteps, span_ms = 200.0, 55.0, 8, 90.0
        span = self._ms_to_frames(span_ms)
        for i in range(nsteps + 1):
            hz = f0 * (f1 / f0) ** (i / nsteps)
            div = max(1, min(4095, int(round(self.clock / (32.0 * hz)))))
            at = frame + int(round(span * i / nsteps))
            if self.scheduler is not None:
                self.scheduler(at, chip, ch, 0x02, div & 0xFF)
                self.scheduler(at, chip, ch, 0x03, (div >> 8) & 0x0F)
            else:
                self._write(chip, ch, 0x02, div & 0xFF)
                self._write(chip, ch, 0x03, (div >> 8) & 0x0F)

        v = self._start_perc_voice(chip, ch, vel, 190.0, 0)
        self._write(chip, ch, 0x04, 0x80)
        self._begin_phase(v, ATTACK, frame)

    def _noise_hit(self, frame: int, vel: int, nf: int, decay: float, peak_off: int) -> None:
        chip, ch = self.drum_noise
        v = self._start_perc_voice(chip, ch, vel, decay, peak_off)
        v.nf = nf & 0x1F
        self._write(chip, ch, 0x07, 0x80 | v.nf)
        self._write(chip, ch, 0x05, 0xFF)
        self._write(chip, ch, 0x04, 0x80)
        self._begin_phase(v, ATTACK, frame)

    # ------------------------------------------------------------------
    # Renderer 接口
    # ------------------------------------------------------------------
    def next_due(self) -> int:
        best = INF
        for v in self.all_voices:
            if v.active and v.next_frame < best:
                best = v.next_frame
        return best

    def advance(self, frame: int) -> None:
        for v in self.all_voices:
            if v.active and v.next_frame <= frame:
                self._advance_voice(v, frame)

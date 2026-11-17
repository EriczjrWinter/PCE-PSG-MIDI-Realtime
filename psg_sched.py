# psg_sched.py
"""采样级调度：MIDI 事件 + (frame, reg, val) 写入队列 + 重采样。

这是 handoff 里"阶段 2"的落点：PCE PSG 没有硬件包络，所有音量/音高变化都得
由软件按采样点写寄存器，所以调度器必须能把"未来的某一次寄存器写入"和 MIDI
事件放在同一条时间轴上切块。

结构上刻意和 OPMMIDI-RT/rt_engine.py 的 Renderer 保持同形（render(n) →
(n,2) int16），区别只有一处：切块边界不只由 MIDI 事件决定，还由**写入队列**
和**驱动的下一次状态变化**决定。
"""
from __future__ import annotations

import ctypes
import heapq
import threading
import time

import numpy as np

SR_OUT_DEFAULT = 48000
INF = 1 << 62


def apply_master(x: np.ndarray, gain: float = 1.0, threshold: float = 0.85) -> np.ndarray:
    """总音量 + 软限幅。播放和导出共用，保证"听到的"和"导出的"一致。"""
    if x.shape[0] == 0:
        return x.astype(np.int16)
    v = np.asarray(x, dtype=np.float32) * np.float32(gain) / np.float32(32768.0)
    a = np.abs(v)
    over = a > threshold
    if np.any(over):
        t = threshold
        y = np.sign(v) * (t + (1.0 - t) * np.tanh((a - t) / (1.0 - t)))
        v = np.where(over, y, v)
    return np.clip(v * 32767.0, -32768, 32767).astype(np.int16)


# --------------------------------------------------------------------------
# 1. 写入队列
# --------------------------------------------------------------------------
class WriteQueue:
    """(frame, chip, reg, val) 优先队列。

    - frame 是芯片采样帧号（不是秒），避免浮点比较；
    - 同一帧的多次写入按入队顺序执行（seq），这一点对"先选通道再写寄存器"
      是必需的；
    - 只做"未来的写入"，立刻生效的写入请直接调用 core.write。
    """

    __slots__ = ("_h", "_seq")

    def __init__(self):
        self._h = []
        self._seq = 0

    def push(self, frame: int, chip: int, reg: int, val: int) -> None:
        heapq.heappush(self._h, (int(frame), self._seq, int(chip), reg & 0x0F, val & 0xFF))
        self._seq += 1

    def next_frame(self) -> int:
        return self._h[0][0] if self._h else INF

    def pop_due(self, frame: int) -> list[tuple[int, int, int]]:
        out = []
        h = self._h
        while h and h[0][0] <= frame:
            _f, _s, chip, reg, val = heapq.heappop(h)
            out.append((chip, reg, val))
        return out

    def __len__(self) -> int:
        return len(self._h)


# --------------------------------------------------------------------------
# 2. 渲染器
# --------------------------------------------------------------------------
class PsgRenderer:
    """把 core + driver 包成"给帧数就出 int16 块"。

    三种时间源合并成一条时间轴：
      1. MIDI 事件（秒 → 帧）
      2. WriteQueue 里的 (frame, reg, val)
      3. driver 自己算出来的下一次包络/音高变化
    """

    MAX_CHUNK = 4096

    def __init__(self, core, driver=None, events=(), total_sec: float = 0.0,
                 tail: float = 1.5):
        self.core = core
        self.driver = driver
        self.sr = core.sample_rate()
        self.total_frames = int(round((total_sec + tail) * self.sr))

        self.ev = [(int(round(e.t * self.sr)), e.kind, e.ch, e.d1, e.d2) for e in events]
        self.ei = 0

        self.frame = 0
        self.done = False
        self.wq = WriteQueue()
        self.dispatch_log = []          # [(帧号, kind)]，自检用

        self._buf = (ctypes.c_int16 * (2 * self.MAX_CHUNK))()

    # -- 写入队列 --
    def schedule(self, frame: int, chip: int, reg: int, val: int) -> None:
        self.wq.push(frame, chip, reg, val)

    def schedule_note(self, frame: int, chip: int, ch: int, reg: int, val: int) -> None:
        """连带通道选择的两次写入。"""
        self.wq.push(frame, chip, 0x00, ch)
        self.wq.push(frame, chip, reg, val)

    # -- 内部 --
    def _driver_due(self) -> int:
        return self.driver.next_due() if self.driver is not None else INF

    def _next_event_frame(self) -> int:
        return self.ev[self.ei][0] if self.ei < len(self.ev) else INF

    def _next_boundary(self) -> int:
        return min(self._next_event_frame(), self.wq.next_frame(), self._driver_due())

    def _run_due(self) -> None:
        """把 <= 当前帧的所有写入和事件都执行掉。"""
        for chip, reg, val in self.wq.pop_due(self.frame):
            self.core.write(reg, val, chip=chip)

        if self.driver is not None:
            self.driver.advance(self.frame)

        while self.ei < len(self.ev) and self.ev[self.ei][0] <= self.frame:
            _f, kind, ch, d1, d2 = self.ev[self.ei]
            self.dispatch_log.append((self.frame, kind))
            self._dispatch(kind, ch, d1, d2)
            self.ei += 1

        # 事件派发本身可能又往里塞了"当前帧"的写入
        for chip, reg, val in self.wq.pop_due(self.frame):
            self.core.write(reg, val, chip=chip)

    def _dispatch(self, kind: str, ch: int, d1: int, d2: int) -> None:
        drv = self.driver
        if kind == "on":
            drv.note_on(self.frame, ch, d1, d2)
        elif kind == "off":
            drv.note_off(self.frame, ch, d1)
        elif kind == "prog":
            drv.program_change(self.frame, ch, d1)
        elif kind == "bend":
            drv.pitch_bend(self.frame, ch, d1, d2)
        elif kind == "cc":
            drv.control_change(self.frame, ch, d1, d2)
        elif kind == "wr":          # 直接寄存器写入，自检/调试用（chip 0）
            self.core.write(0x00, ch)
            self.core.write(d1, d2)

    def _chunk(self, n: int) -> np.ndarray:
        self.core.generate_into(n, self._buf)
        return np.frombuffer(self._buf, dtype="<i2", count=2 * n).reshape(n, 2)

    # -- 对外 --
    def render(self, n: int) -> np.ndarray:
        out = np.zeros((n, 2), np.int16)
        if self.frame >= self.total_frames:
            self.done = True
            return out

        n_real = min(n, self.total_frames - self.frame)
        pos = 0
        guard = 0
        while pos < n_real:
            b = self._next_boundary()
            if b <= self.frame:
                self._run_due()
                guard += 1
                if self._next_boundary() <= self.frame:
                    raise RuntimeError(
                        f"调度器没有前进（frame={self.frame}, guard={guard}）："
                        "检查 driver 是否在同一个 frame 上重复排程")
                continue
            take = min(n_real - pos, b - self.frame, self.MAX_CHUNK)
            out[pos:pos + take] = self._chunk(take)
            self.frame += take
            pos += take

        if self.frame >= self.total_frames:
            self.done = True
        return out


# --------------------------------------------------------------------------
# 3. 重采样：557xx → 48000，4 点 Catmull-Rom
#    （与 OPMMIDI-RT/rt_engine.py 同款；这里因为只有一个声源，直接照搬）
# --------------------------------------------------------------------------
class Resampler:
    def __init__(self, sr_in: float, sr_out: float):
        self.step = float(sr_in) / float(sr_out)
        self.buf = np.zeros((1, 2), np.float32)
        self.pos = 1.0

    def process(self, block: np.ndarray) -> np.ndarray:
        b = np.asarray(block, dtype=np.float32)
        if b.shape[0]:
            self.buf = np.concatenate((self.buf, b))

        n = self.buf.shape[0]
        if n < 4 or self.pos + 2 >= n:
            return np.zeros((0, 2), np.int16)

        m = int(np.floor((n - 3 - self.pos) / self.step)) + 1
        if m <= 0:
            return np.zeros((0, 2), np.int16)

        p = self.pos + self.step * np.arange(m, dtype=np.float64)
        i = p.astype(np.int64)
        t = (p - i).astype(np.float32)[:, None]

        x0 = self.buf[i - 1]
        x1 = self.buf[i]
        x2 = self.buf[i + 1]
        x3 = self.buf[i + 2]

        c0 = x1
        c1 = 0.5 * (x2 - x0)
        c2 = x0 - 2.5 * x1 + 2.0 * x2 - 0.5 * x3
        c3 = -0.5 * x0 + 1.5 * x1 - 1.5 * x2 + 0.5 * x3
        y = ((c3 * t + c2) * t + c1) * t + c0

        self.pos += self.step * m
        drop = int(np.floor(self.pos)) - 1
        if drop > 0:
            self.buf = self.buf[drop:]
            self.pos -= drop

        return np.clip(y, -32768, 32767).astype(np.int16)


# --------------------------------------------------------------------------
# 4. 生产者线程（实时播放用，阶段 6 才会真正接上声卡）
# --------------------------------------------------------------------------
class Engine:
    CHIP_CHUNK = 1024

    def __init__(self, renderer, sr_out: float = SR_OUT_DEFAULT,
                 blocksize: int = 256, target_blocks: int = 4,
                 master: float = 1.0, chip_chunk: int | None = None):
        self.r = renderer
        if chip_chunk:
            self.CHIP_CHUNK = int(chip_chunk)
        self.res = Resampler(renderer.sr, sr_out)
        self.sr_out = sr_out
        self.blocksize = blocksize
        self.target = blocksize * target_blocks
        self.master = float(master)

        remaining = max(0, renderer.total_frames - renderer.frame)
        self.total_out = int(round(remaining * sr_out / renderer.sr))
        self.emitted = 0
        self.producer_done = False
        self.underruns = 0

        self._acc = np.zeros((0, 2), np.int16)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        t = threading.Thread(target=self._run, name="pce-producer", daemon=True)
        self._thread = t
        t.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

    def _run(self):
        while not self._stop.is_set():
            if self.producer_done:
                return
            with self._lock:
                starving = self._acc.shape[0] < self.target
            if not starving:
                time.sleep(0.002)
                continue

            chip = self.r.render(self.CHIP_CHUNK)
            out = self.res.process(chip)
            if out.shape[0]:
                # 总音量和软限幅放在生产者线程里做，别占音频回调的时间
                out = apply_master(out, self.master)

            with self._lock:
                if out.shape[0]:
                    self._acc = np.concatenate((self._acc, out))
                if self.r.done:
                    self.producer_done = True

            if out.shape[0] == 0 and self.r.done:
                return

    def pull(self, n: int) -> np.ndarray:
        with self._lock:
            if self._acc.shape[0] >= n:
                blk = self._acc[:n]
                self._acc = self._acc[n:]
                self.emitted += n
                return blk
            have = self._acc.shape[0]
            blk = np.zeros((n, 2), np.int16)
            if have:
                blk[:have] = self._acc
                self._acc = np.zeros((0, 2), np.int16)
            if self.emitted + have < self.total_out:
                self.underruns += 1
            self.emitted += n
            return blk

    def finished(self) -> bool:
        return self.emitted >= self.total_out

    def buffered_frames(self) -> int:
        with self._lock:
            return self._acc.shape[0]

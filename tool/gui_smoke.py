# tool/gui_smoke.py
"""无人值守地跑一遍 GUI 的主要路径（不截图、不点鼠标）。

用法：python tool/gui_smoke.py [midi]
验证：建窗 → 播放 → 暂停/继续 → 点击进度条跳转 → 切语言 → 停止。
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from app_pce import App                      # noqa: E402
from pcepsg_session import Config            # noqa: E402

MIDI = sys.argv[1] if len(sys.argv) > 1 else r"D:\music\ys4field.mid"


class Ev:
    def __init__(self, x):
        self.x = x


def pump(app: App, seconds: float) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.root.update()
        time.sleep(0.02)


def main() -> int:
    cfg = Config.load()
    cfg.midi = MIDI
    cfg.volume = 0.0          # 静音跑，别吓到人
    cfg.chips = 4
    cfg.lang = "zh"
    cfg.loop = False

    app = App(cfg)
    app.root.update()
    print("window ok:", app.root.title())

    app.on_play()
    pump(app, 2.0)
    s = app.session
    assert s is not None, "session 没建起来"
    print(f"playing: pos={s.position:.2f}s underrun={s.underruns} xrun={s.xruns}")
    assert s.position > 1.0, "播放进度没有前进"

    app.on_pause()
    pump(app, 0.4)
    p_paused = s.position
    pump(app, 0.5)
    print(f"paused: pos={p_paused:.2f} -> {s.position:.2f}（应基本不动）")
    assert abs(s.position - p_paused) < 0.15, "暂停后进度还在跑"

    app.on_pause()            # 继续
    pump(app, 0.5)
    assert s.position > p_paused + 0.2, "继续播放失败"
    print("resume ok")

    app.on_seek_click(Ev(int(app.pbar.winfo_width() * 0.5)))
    pump(app, 0.8)
    print(f"seek to 50%: pos={s.position:.2f}s")
    assert s.position >= s.total_sec * 0.45, "跳转没生效"

    app.v_lang.set("English")
    app.on_lang()
    pump(app, 0.3)
    print("lang switch ok:", app.root.title())
    app.v_lang.set("中文")
    app.on_lang()
    pump(app, 0.2)

    app.on_stop()
    pump(app, 0.2)
    print("stop ok")

    # 真正关闭
    app.on_close()
    print("PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

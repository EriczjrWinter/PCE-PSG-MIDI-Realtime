# NOTICE

本仓库的代码以 **MIT** 发布，全文见 [LICENSE](LICENSE)。

下面列出与之相关的第三方内容——它们**不**在 MIT 覆盖范围内，各自遵循原始许可证。
The code in this repository is released under the **MIT** license (see
[LICENSE](LICENSE)). The third-party material below keeps its own terms.

---

## 1. 参考实现（随仓库保存，不参与编译）

| 文件 | 来源 | 许可证 |
|---|---|---|
| `ref/c6280.cpp` | [MAME](https://github.com/mamedev/mame) `src/devices/sound/c6280.cpp` —— Hudson PSG 模拟器，© Charles MacDonald 与 MAME 团队 | **BSD-3-Clause** |

这份文件只作为**行为参考**保存在仓库里，用于核对寄存器语义与计数器行为。

`core/pcepsg.c` 是针对同一份行为规格的**独立实现**：`pcepsg.dll` 不含任何来自
MAME 的代码，也不链接 MAME 的任何部分，因此 core 部分的许可与 MAME 无关。
`ref/c6280.cpp` 的文件头已保留原始版权与许可证声明，未作修改。

## 2. 运行依赖

| 组件 | 许可证 |
|---|---|
| [numpy](https://numpy.org/) | BSD-3-Clause |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) | MIT |
| PortAudio（随 sounddevice 的 wheel 分发） | MIT |

这些依赖由用户从 PyPI 安装，不随本仓库或 EXE 的源代码分发。打包出的单文件 EXE
会把它们一并封进去，各自的许可证仍然有效。

## 3. 构建工具（不随发布物分发）

| 组件 | 许可证 |
|---|---|
| [PyInstaller](https://pyinstaller.org/) | GPL-2.0 + 例外条款（打包产物不受 GPL 约束） |
| [w64devkit](https://github.com/skeeto/w64devkit) / GCC | GPL-3.0 + 运行时例外（仅用于编译 `pcepsg.dll`） |

## 4. 不含任何主机数据

本仓库与发布物**不包含**任何来自 PC Engine / TurboGrafx-16 的 ROM、BIOS、采样或
固件数据，也不包含任何鼓采样或第三方录音。所有打击乐都是运行时合成的
（扫频底鼓 + LFSR 噪声）。

`pcew.py` 里的 32 步波表是按数学公式生成的常量（正弦、方波、锯齿、三角及其谐波
组合），不是从任何游戏或音色库中转储的。

No ROM, BIOS, sample or firmware data from any console is included — in the
repository or in the released EXE. Every wavetable in `pcew.py` is generated
from a mathematical formula, and all percussion is synthesized at runtime.

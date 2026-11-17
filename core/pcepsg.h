/* pcepsg.h —— HuC6280 PSG（PC Engine）行为级核心
 *
 * 行为参考：MAME src/devices/sound/c6280.cpp（BSD-3-Clause, Charles MacDonald）。
 * 本文件为独立重写，只保留"芯片本身"的语义，不含 MAME 的设备框架代码。
 *
 * 关键点：MAME 里 stream 采样率 == PSG 时钟，也就是"1 个输出采样 = 1 个 PSG 时钟"。
 * 我们不需要 3.58 MHz 的输出，所以引入 div：1 个输出采样 = div 个 PSG 时钟。
 * 分频器/噪声/LFO 的计数器都按"时钟"为单位递减 div，因此音高与 div 无关。
 */
#ifndef PCEPSG_H
#define PCEPSG_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* PC Engine 的 PSG 时钟（= CPU 7.15909 MHz / 2）。VGM 里 HuC6280 的 clock 也是它。 */
#define PCEPSG_CLOCK_PCE 3579545

/* 默认下分频：3579545 / 64 = 55930.39 Hz，和 OPM 的 clock/64 对齐，
 * 这样混音时不需要额外重采样。 */
#define PCEPSG_DIV_DEFAULT 64

typedef struct pcepsg pcepsg;

pcepsg  *pcepsg_create(int clock, int div);
void     pcepsg_destroy(pcepsg *p);
void     pcepsg_reset(pcepsg *p);

/* reg 为偏移 0x00..0x0f（即内存映射 $0800 + reg）。 */
void     pcepsg_write(pcepsg *p, int reg, int val);

/* 生成 frames 帧交错立体声 int16（L,R,L,R...）。 */
void     pcepsg_generate(pcepsg *p, int16_t *out, int frames);

/* 输出隔直（模拟耦合电容），默认开。测量芯片的原始直流时关掉。 */
void     pcepsg_set_dc_block(pcepsg *p, int on);

double   pcepsg_sample_rate(const pcepsg *p);
int      pcepsg_div(const pcepsg *p);

/* 调试/自检用：返回某通道当前的 12-bit 分频值。 */
int      pcepsg_channel_freq(const pcepsg *p, int ch);

/* --------------------------------------------------------------------------
 * 多芯片机架：N 颗 PSG 叠成一个声源，得到 6N 个通道。
 * 每颗芯片的行为与单颗完全一致，只是混音时相加（增益默认 1/sqrt(N)）。
 * -------------------------------------------------------------------------- */
typedef struct pcepsg_bank pcepsg_bank;

pcepsg_bank *pcepsg_bank_create(int chips, int clock, int div);
void     pcepsg_bank_destroy(pcepsg_bank *b);
void     pcepsg_bank_reset(pcepsg_bank *b);
void     pcepsg_bank_write(pcepsg_bank *b, int chip, int reg, int val);
void     pcepsg_bank_generate(pcepsg_bank *b, int16_t *out, int frames);
void     pcepsg_bank_set_gain(pcepsg_bank *b, double gain);
void     pcepsg_bank_set_dc_block(pcepsg_bank *b, int on);
double   pcepsg_bank_sample_rate(const pcepsg_bank *b);
int      pcepsg_bank_chips(const pcepsg_bank *b);

#ifdef __cplusplus
}
#endif

#endif /* PCEPSG_H */

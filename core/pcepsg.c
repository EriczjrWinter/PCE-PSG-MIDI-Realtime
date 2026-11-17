/* pcepsg.c —— HuC6280 PSG 行为级实现（PC Engine / TurboGrafx-16）
 *
 * 行为参考：MAME src/devices/sound/c6280.cpp
 *           (BSD-3-Clause, copyright-holders: Charles MacDonald)
 * 这里只复刻芯片语义，不含 MAME 的设备/流框架代码。
 *
 * 采样率模型
 * ----------
 * MAME 里 c6280 的 stream 采样率 == PSG 时钟，即 1 输出采样 = 1 个 PSG 时钟。
 * 3.58 MHz 的输出没有意义，所以引入 div：1 个输出采样 = div 个 PSG 时钟。
 * 所有计数器（波形、噪声、LFO）都以"时钟"为单位，每个采样递减 div，
 * 因此**音高与 div 完全无关**，div 只影响输出采样率与相位量化粒度。
 *
 * 音高
 * ----
 *   f = PSG_CLOCK / (32 * F)     F = 12-bit 分频值；F == 0 当作 4096
 * 这与 Hudson 手册一致，也与 MAME 的计数器行为一致（每个周期正好 F 个时钟）。
 */
#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "pcepsg.h"

#define PCEPSG_NCH 8   /* 通道选择寄存器只有 3 bit，6/7 是空槽，但写进去不能越界 */

typedef struct
{
	uint16_t frequency;      /* 12-bit 分频 */
	uint8_t  control;        /* R4 */
	uint8_t  balance;        /* R5 */
	uint8_t  waveform[32];
	uint8_t  index;
	uint8_t  dda;
	uint8_t  noise_control;  /* R7 */
	int32_t  noise_counter;  /* 单位：PSG 时钟 */
	uint32_t noise_seed;
	int32_t  tick;           /* 距离下一次波形步进还剩多少 PSG 时钟 */
} pcepsg_channel;

struct pcepsg
{
	int      clock;
	int      div;
	int      select;
	uint32_t enabled;        /* bit0..5 == R4 bit7 of each channel */
	uint8_t  balance;        /* R1 */
	uint8_t  lfo_frequency;  /* R8 */
	uint8_t  lfo_control;    /* R9 */
	uint16_t volume_table[32];

	/* 输出耦合电容（一阶高通）。真机的模拟输出有隔直电容，
	 * 而 5-bit 波表以 15.5 为中心、代码里减 16，天然残留 -0.5 LSB 直流。
	 * 不做隔直的话，每次音量变化都会变成一个直流阶跃 == 咔哒声。 */
	int      dc_on;
	double   dc_a;
	double   dc_x[2];
	double   dc_y[2];

	pcepsg_channel ch[PCEPSG_NCH];
};

/* 4-bit 音量 → 5-bit 衰减指数的映射（硬件就是这张怪表，不是线性的） */
static const uint8_t pcepsg_scale_tab[16] =
{
	0x00, 0x03, 0x05, 0x07, 0x09, 0x0b, 0x0d, 0x0f,
	0x10, 0x13, 0x15, 0x17, 0x19, 0x1b, 0x1d, 0x1f
};

static inline int16_t pcepsg_clip16(int32_t v)
{
	if (v > 32767) return 32767;
	if (v < -32768) return -32768;
	return (int16_t)v;
}

static inline uint32_t pcepsg_lfsr_next(uint32_t seed)
{
	/* Charles MacDonald 逆向出来的抽头：bit 0,1,11,12,17 */
	uint32_t feedback = (seed ^ (seed >> 1) ^ (seed >> 11) ^ (seed >> 12) ^ (seed >> 17)) & 1u;
	return (seed >> 1) | (feedback << 17);
}

pcepsg *pcepsg_create(int clock, int div)
{
	if (clock <= 0) clock = PCEPSG_CLOCK_PCE;
	if (div <= 0) div = PCEPSG_DIV_DEFAULT;

	pcepsg *p = (pcepsg *)calloc(1, sizeof(pcepsg));
	if (!p) return NULL;

	p->clock = clock;
	p->div = div;

	/* 音量表：PSG 的 48 dB 动态范围摊在 32 个档位上（1.5 dB/档）。
	 * 系数 65535/6/32 让 6 个通道满幅叠加正好接近 int16 上限。 */
	double level = 65535.0 / 6.0 / 32.0;
	const double step = 48.0 / 32.0;
	for (int i = 0; i < 31; i++)
	{
		p->volume_table[i] = (uint16_t)level;
		level /= pow(10.0, step / 20.0);
	}
	p->volume_table[31] = 0;   /* 最弱一档直接给 0，避免 -1 LSB 的残余 */

	/* 隔直：截止频率固定 5 Hz，与 div 无关 */
	p->dc_on = 1;
	p->dc_a = 1.0 - (2.0 * 3.14159265358979323846 * 8.0) / ((double)clock / (double)div);

	pcepsg_reset(p);
	return p;
}

void pcepsg_destroy(pcepsg *p)
{
	free(p);
}

void pcepsg_reset(pcepsg *p)
{
	if (!p) return;
	p->select = 0;
	p->enabled = 0;
	p->balance = 0;
	p->lfo_frequency = 0;
	p->lfo_control = 0;
	p->dc_x[0] = p->dc_x[1] = 0.0;
	p->dc_y[0] = p->dc_y[1] = 0.0;
	memset(p->ch, 0, sizeof(p->ch));
	for (int i = 0; i < PCEPSG_NCH; i++)
	{
		if (i >= 4) p->ch[i].noise_seed = 1;
	}
}

void pcepsg_set_dc_block(pcepsg *p, int on)
{
	if (!p) return;
	p->dc_on = on ? 1 : 0;
	p->dc_x[0] = p->dc_x[1] = 0.0;
	p->dc_y[0] = p->dc_y[1] = 0.0;
}

void pcepsg_write(pcepsg *p, int reg, int val)
{
	if (!p) return;
	pcepsg_channel *chan = &p->ch[p->select & 0x07];
	const uint8_t data = (uint8_t)(val & 0xff);

	switch (reg & 0x0f)
	{
	case 0x00: /* 通道选择 */
		p->select = data & 0x07;
		break;

	case 0x01: /* 总音量 L/R */
		p->balance = data;
		break;

	case 0x02: /* 分频低 8 位 */
		chan->frequency = (uint16_t)((chan->frequency & 0x0f00) | data);
		break;

	case 0x03: /* 分频高 4 位 */
		chan->frequency = (uint16_t)((chan->frequency & 0x00ff) | ((data << 8) & 0x0f00));
		break;

	case 0x04: /* 通道控制：使能 / DDA / 5-bit 音量 */
		/* DDA 位 1→0 会把波形指针清 0（硬件行为） */
		if ((chan->control & 0x40) && !(data & 0x40))
			chan->index = 0;
		/* 使能位 0→1 会重装计数器 == key-on 的相位复位 */
		if (!(chan->control & 0x80) && (data & 0x80))
			chan->tick = chan->frequency ? chan->frequency : 0x1000;
		chan->control = data;

		p->enabled &= ~(1u << p->select);
		p->enabled |= (uint32_t)((data >> 7) & 1) << p->select;
		break;

	case 0x05: /* 通道 L/R 平衡 */
		chan->balance = data;
		break;

	case 0x06: /* 波表数据 / DDA 采样 */
		if (chan->control & 0x40)
		{
			chan->dda = data & 0x1f;
		}
		else
		{
			chan->waveform[chan->index & 0x1f] = data & 0x1f;
			/* 只有通道关闭时写入才自动前进（真机行为，也是批量上传波表的依据） */
			if (!(chan->control & 0x80))
				chan->index = (uint8_t)((chan->index + 1) & 0x1f);
		}
		break;

	case 0x07: /* 噪声：仅通道 4/5 有效 */
		chan->noise_control = data;
		break;

	case 0x08: /* LFO 频率 */
		p->lfo_frequency = data;
		break;

	case 0x09: /* LFO 控制 */
		p->lfo_control = data;
		break;

	default:
		break;
	}
}

void pcepsg_generate(pcepsg *p, int16_t *out, int frames)
{
	if (!p || frames <= 0) return;

	/* 没有通道使能时直接出静音（MAME 也有这条快路径） */
	if (!(p->enabled & 0x3f))
	{
		memset(out, 0, (size_t)frames * 2 * sizeof(int16_t));
		return;
	}

	const int div = p->div;
	const uint8_t lmal = pcepsg_scale_tab[(p->balance >> 4) & 0x0f];
	const uint8_t rmal = pcepsg_scale_tab[(p->balance >> 0) & 0x0f];

	for (int i = 0; i < frames; i++)
	{
		int32_t lout = 0, rout = 0;

		for (int c = 0; c < 6; c++)
		{
			if (!(p->enabled & (1u << c)))
				continue;

			pcepsg_channel *chan = &p->ch[c];

			const uint8_t lal = pcepsg_scale_tab[(chan->balance >> 4) & 0x0f];
			const uint8_t ral = pcepsg_scale_tab[(chan->balance >> 0) & 0x0f];
			const uint8_t al = chan->control & 0x1f;

			/* 三级衰减指数相加后钳在 31（静音） */
			int vll = (0x1f - lmal) + (0x1f - al) + (0x1f - lal);
			if (vll > 0x1f) vll = 0x1f;
			int vlr = (0x1f - rmal) + (0x1f - al) + (0x1f - ral);
			if (vlr > 0x1f) vlr = 0x1f;

			vll = p->volume_table[vll];
			vlr = p->volume_table[vlr];

			int16_t data;

			if (c >= 4 && (chan->noise_control & 0x80))
			{
				/* ---- 噪声模式 ---- */
				const uint32_t step = (chan->noise_control & 0x1f) ^ 0x1f;
				data = (chan->noise_seed & 1) ? 0x1f : 0;

				chan->noise_counter -= div;
				if (chan->noise_counter <= 0)
				{
					if (step)
						chan->noise_counter += (int32_t)(step << 6);
					else
						chan->noise_counter = 0;
					chan->noise_seed = pcepsg_lfsr_next(chan->noise_seed);
				}
			}
			else if (chan->control & 0x40)
			{
				/* ---- DDA 模式：直接输出写入的 5-bit 值 ---- */
				data = chan->dda;
			}
			else if ((p->lfo_control & 3) && c < 2)
			{
				/* ---- 波形模式 + LFO（LFO 只调变通道 0，波形源是通道 1） ---- */
				if (c == 0)
				{
					pcepsg_channel *src = &p->ch[1];
					pcepsg_channel *dst = &p->ch[0];
					const int32_t lfo_step = src->frequency ? src->frequency : 0x1000;
					int32_t step = dst->frequency ? dst->frequency : 0x1000;

					if (p->lfo_control & 0x80)
					{
						/* LFO 复位位：每个采样都重置，等于 LFO 停住 */
						src->tick = lfo_step * p->lfo_frequency;
						src->index = 0;
					}
					else
					{
						const int16_t lfo_data = src->waveform[src->index];
						src->tick -= div;
						if (src->tick <= 0)
						{
							src->tick = lfo_step * p->lfo_frequency;
							src->index = (uint8_t)((src->index + 1) & 0x1f);
						}
						step += (int32_t)(lfo_data - 16) << (((p->lfo_control & 3) - 1) << 1);
					}
					if (step < 1) step = 1;   /* 硬件上会卡死，这里兜底 */

					data = dst->waveform[dst->index];
					dst->tick -= div;
					if (dst->tick <= 0)
					{
						dst->tick += step;
						dst->index = (uint8_t)((dst->index + 1) & 0x1f);
					}
				}
				else
				{
					/* 通道 1 在 LFO 开启时不静音，也不自己步进 */
					data = chan->waveform[chan->index];
				}
			}
			else
			{
				/* ---- 普通波形模式 ---- */
				const int32_t step = chan->frequency ? chan->frequency : 0x1000;
				data = chan->waveform[chan->index];

				chan->tick -= div;
				while (chan->tick <= 0)
				{
					chan->tick += step;
					chan->index = (uint8_t)((chan->index + 1) & 0x1f);
				}
			}

			lout += vll * (data - 16);
			rout += vlr * (data - 16);
		}

		if (p->dc_on)
		{
			const double yl = p->dc_a * (p->dc_y[0] + (double)lout - p->dc_x[0]);
			const double yr = p->dc_a * (p->dc_y[1] + (double)rout - p->dc_x[1]);
			p->dc_x[0] = (double)lout;
			p->dc_x[1] = (double)rout;
			p->dc_y[0] = yl;
			p->dc_y[1] = yr;
			lout = (int32_t)(yl >= 0.0 ? yl + 0.5 : yl - 0.5);
			rout = (int32_t)(yr >= 0.0 ? yr + 0.5 : yr - 0.5);
		}

		out[2 * i + 0] = pcepsg_clip16(lout);
		out[2 * i + 1] = pcepsg_clip16(rout);
	}
}

double pcepsg_sample_rate(const pcepsg *p)
{
	return (double)p->clock / (double)p->div;
}

int pcepsg_div(const pcepsg *p)
{
	return p->div;
}

int pcepsg_channel_freq(const pcepsg *p, int ch)
{
	if (ch < 0 || ch >= PCEPSG_NCH) return 0;
	return p->ch[ch].frequency;
}


/* ==========================================================================
 * 多芯片（机架）
 * --------------------------------------------------------------------------
 * PC Engine 只有一颗 HuC6280，也就是只有 6 个通道 —— 这是历史事实。
 * 但"用 PSG 的音色做 MIDI 合成器"不必被一颗芯片绑死：这里把 N 颗 PSG
 * 当成一个机架来混音，得到 6N 个通道。每颗芯片仍然严格按 6 通道行为运行，
 * 只是混音时叠加。这是"四台 PCE 一起响"，不是"魔改芯片"。
 * ========================================================================== */
struct pcepsg_bank
{
	int      n;
	double   gain;
	pcepsg **chip;
	int32_t *acc;
	int16_t *tmp;
	int      cap;
};

pcepsg_bank *pcepsg_bank_create(int chips, int clock, int div)
{
	if (chips < 1) chips = 8;
	if (clock <= 0) clock = PCEPSG_CLOCK_PCE;
	if (div <= 0) div = PCEPSG_DIV_DEFAULT;

	pcepsg_bank *b = (pcepsg_bank *)calloc(1, sizeof(pcepsg_bank));
	if (!b) return NULL;

	b->n = chips;
	b->gain = 1.0 / sqrt((double)chips);
	b->chip = (pcepsg **)calloc((size_t)chips, sizeof(pcepsg *));
	if (!b->chip) { free(b); return NULL; }

	for (int i = 0; i < chips; i++)
	{
		b->chip[i] = pcepsg_create(clock, div);
		if (!b->chip[i])
		{
			pcepsg_bank_destroy(b);
			return NULL;
		}
	}
	return b;
}

void pcepsg_bank_destroy(pcepsg_bank *b)
{
	if (!b) return;
	if (b->chip)
	{
		for (int i = 0; i < b->n; i++)
			pcepsg_destroy(b->chip[i]);
		free(b->chip);
	}
	free(b->acc);
	free(b->tmp);
	free(b);
}

void pcepsg_bank_reset(pcepsg_bank *b)
{
	if (!b) return;
	for (int i = 0; i < b->n; i++)
		pcepsg_reset(b->chip[i]);
}

void pcepsg_bank_write(pcepsg_bank *b, int chip, int reg, int val)
{
	if (!b || chip < 0 || chip >= b->n) return;
	pcepsg_write(b->chip[chip], reg, val);
}

void pcepsg_bank_set_gain(pcepsg_bank *b, double gain)
{
	if (b) b->gain = gain;
}

void pcepsg_bank_set_dc_block(pcepsg_bank *b, int on)
{
	if (!b) return;
	for (int i = 0; i < b->n; i++)
		pcepsg_set_dc_block(b->chip[i], on);
}

double pcepsg_bank_sample_rate(const pcepsg_bank *b)
{
	return b && b->n ? pcepsg_sample_rate(b->chip[0]) : 0.0;
}

int pcepsg_bank_chips(const pcepsg_bank *b)
{
	return b ? b->n : 0;
}

void pcepsg_bank_generate(pcepsg_bank *b, int16_t *out, int frames)
{
	if (!b || frames <= 0) return;

	if (frames > b->cap)
	{
		int cap = frames + 1024;
		int32_t *acc = (int32_t *)realloc(b->acc, (size_t)cap * 2 * sizeof(int32_t));
		int16_t *tmp = (int16_t *)realloc(b->tmp, (size_t)cap * 2 * sizeof(int16_t));
		if (!acc || !tmp)
		{
			free(acc ? acc : b->acc);
			free(tmp ? tmp : b->tmp);
			b->acc = NULL;
			b->tmp = NULL;
			b->cap = 0;
			memset(out, 0, (size_t)frames * 2 * sizeof(int16_t));
			return;
		}
		b->acc = acc;
		b->tmp = tmp;
		b->cap = cap;
	}

	memset(b->acc, 0, (size_t)frames * 2 * sizeof(int32_t));

	for (int c = 0; c < b->n; c++)
	{
		pcepsg_generate(b->chip[c], b->tmp, frames);
		for (int i = 0; i < frames * 2; i++)
			b->acc[i] += b->tmp[i];
	}

	const double g = b->gain;
	for (int i = 0; i < frames * 2; i++)
	{
		double y = (double)b->acc[i] * g;
		int32_t v = (int32_t)(y >= 0.0 ? y + 0.5 : y - 0.5);
		out[i] = pcepsg_clip16(v);
	}
}

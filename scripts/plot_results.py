"""Generate the README figures from the published records. Requires Matplotlib."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

import matplotlib

matplotlib.use('Agg')

import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import MaxNLocator, PercentFormatter

ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / 'results/formal-run/provenance/records'
FIGURES = ROOT / 'docs/figures'
LABELS = ('实证', '理论+实证', '理论', '描述性统计和综述', '无法判断')
BLUE = '#235C8C'
ORANGE = '#AB4C2C'
INK = '#202931'
GRID = '#E4E8EC'


def set_style(font_path):
    fonts = (
        'Noto Sans CJK SC', 'Source Han Sans SC', 'Hiragino Sans GB',
        'PingFang SC', 'Microsoft YaHei', 'WenQuanYi Zen Hei',
    )
    if font_path:
        font_manager.fontManager.addfont(font_path)
        family = font_manager.FontProperties(fname=font_path).get_name()
    else:
        installed = {entry.name for entry in font_manager.fontManager.ttflist}
        family = next((name for name in fonts if name in installed), None)
        if family is None:
            raise SystemExit('找不到中文字体。请安装 Noto Sans CJK SC 或使用 --font 指定字体。')

    plt.rcParams.update({
        'font.family': [family, 'DejaVu Sans'],
        'font.size': 10,
        'axes.titlesize': 12,
        'axes.labelcolor': INK,
        'axes.spines.top': False,
        'axes.spines.right': False,
        'axes.edgecolor': '#A4ADB5',
        'axes.linewidth': 0.7,
        'text.color': INK,
        'xtick.color': INK,
        'ytick.color': INK,
        'grid.color': GRID,
        'grid.linewidth': 0.7,
        'axes.axisbelow': True,
        'legend.frameon': False,
        'savefig.dpi': 220,
        'savefig.facecolor': 'white',
    })


def save(figure, name):
    FIGURES.mkdir(parents=True, exist_ok=True)
    figure.savefig(FIGURES / name)
    plt.close(figure)


def label_agreement(records, categories, by_paper):
    figure, (frequency, agreement) = plt.subplots(
        ncols=2, figsize=(10.5, 3.7), width_ratios=(1.05, 1), layout='constrained',
    )
    figure.get_layout_engine().set(w_pad=0.12, h_pad=0.09, wspace=0.12)
    votes = Counter(record['label_one'] for record in records)
    counts = [votes[category] for category in categories]
    bars = frequency.barh(categories, counts, height=0.56, color=BLUE)
    frequency.bar_label(
        bars, labels=[f'{count}  ({count / len(records):.0%})' for count in counts],
        padding=5, fontsize=9,
    )
    frequency.invert_yaxis()
    frequency.set(xlim=(0, max(counts) * 1.29), xlabel=f'原判份数（n = {len(records)}）')
    frequency.xaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))
    frequency.grid(axis='x')
    frequency.tick_params(axis='y', length=0)
    frequency.set_title('(a)  标签一频数', loc='left', pad=10)

    shares = {}
    for key in ('label_one', 'label_two'):
        shares[key] = Counter(
            round(100 * Counter(record[key] for record in group).most_common(1)[0][1] / len(group))
            for group in by_paper.values()
        )
    levels = sorted(set(shares['label_one']) | set(shares['label_two']))
    positions = list(range(len(levels)))
    for key, label, shift, color in (
        ('label_one', '标签一', -0.18, BLUE), ('label_two', '标签二', 0.18, ORANGE),
    ):
        counts = [shares[key][level] for level in levels]
        bars = agreement.bar(
            [x + shift for x in positions], counts, width=0.34, label=label, color=color,
        )
        agreement.bar_label(bars, labels=[str(count) if count else '' for count in counts], padding=3, fontsize=9)
    agreement.set_xticks(positions, [f'{level}%' for level in levels])
    agreement.set(
        xlim=(-0.52, len(levels) - 0.48), ylim=(0, len(by_paper) * 1.17),
        xlabel='每篇论文的众数票数占比', ylabel=f'论文篇数（n = {len(by_paper)}）',
    )
    agreement.yaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))
    agreement.grid(axis='y')
    agreement.legend(loc='upper left', fontsize=9)
    agreement.set_title('(b)  一致率分布', loc='left', pad=10)
    save(figure, 'label-agreement.png')


def source_label_distribution(records, categories):
    votes = defaultdict(Counter)
    papers = defaultdict(set)
    for record in records:
        mode = tuple(record['modes'])
        votes[mode][record['label_one']] += 1
        papers[mode].add(record['paper']['id'])

    figure, axes = plt.subplots(figsize=(8.5, 3.7), layout='constrained')
    figure.get_layout_engine().set(w_pad=0.13, h_pad=0.09)
    rows = list(range(len(categories)))
    max_share = 0
    for mode, name, shift, marker, color in (
        (('pdf',), 'PDF 文本', -0.15, 'o', BLUE),
        (('md',), 'MD 文本', 0.15, 's', ORANGE),
    ):
        total = votes[mode].total()
        raw_shares = [votes[mode][category] / total for category in categories]
        positions = [row + shift for row in rows]
        max_share = max(max_share, *raw_shares)
        axes.scatter(
            raw_shares, positions, s=36, marker=marker, color=color, zorder=3,
            label=f'{name}（{len(papers[mode])} 篇，{total} 份）',
        )
        for share, y in zip(raw_shares, positions):
            axes.annotate(
                f'{share:.1%}', (share, y), xytext=(7, 0), textcoords='offset points',
                va='center', fontsize=9, color=color,
            )

    axes.set_yticks(rows, categories)
    axes.set(ylim=(len(rows) - 0.55, -0.55), xlim=(-0.025, max_share + 0.11), xlabel='占该来源原判的比例')
    axes.xaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    axes.grid(axis='x')
    axes.tick_params(axis='y', length=0)
    axes.spines['left'].set_visible(False)
    axes.legend(loc='lower left', bbox_to_anchor=(-0.01, 1.00), ncols=2, columnspacing=1.4, fontsize=9)
    axes.set_title('按判读材料分组的标签一构成', loc='left', pad=32)
    save(figure, 'source-label-distribution.png')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', type=Path, help='Optional path to a Chinese font file.')
    args = parser.parse_args()
    set_style(args.font)
    records = [json.loads(path.read_text(encoding='utf-8')) for path in sorted(RECORDS.glob('*/*.json'))]
    if not records:
        raise SystemExit(f'未找到原判 JSON：{RECORDS}')
    by_paper = defaultdict(list)
    for record in records:
        by_paper[record['paper']['id']].append(record)
    observed = {record['label_one'] for record in records}
    categories = [label for label in LABELS if label in observed]
    label_agreement(records, categories, by_paper)
    source_label_distribution(records, categories)


if __name__ == '__main__':
    main()

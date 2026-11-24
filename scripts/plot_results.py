"""Generate model comparison figures from the published judgments. Requires Matplotlib."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

import matplotlib

matplotlib.use('Agg')

import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.ticker import MultipleLocator

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'results/step-evidence-comparison'
FIGURES = ROOT / 'docs/figures'
MODELS = (
    ('deepseek-v4.1-flash', 'DeepSeek V4.1 Flash', '#216080', 'o'),
    ('gpt-6.2-sol', 'GPT-6.2-sol', '#AB4C2C', 'D'),
)
LABELS = (
    ('label_one', '标签一 · 研究类型', ('实证', '理论+实证', '理论', '描述性统计和综述', '无法判断')),
    ('label_two', '标签二 · 经管属性', ('核心经管', '广义经管')),
)
INK = '#202931'
MUTED = '#53616D'


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
        'font.size': 11,
        'axes.titlesize': 13,
        'axes.labelcolor': INK,
        'axes.spines.top': False,
        'axes.spines.right': False,
        'axes.spines.left': False,
        'axes.edgecolor': '#A4ADB5',
        'text.color': INK,
        'xtick.color': MUTED,
        'ytick.color': INK,
        'grid.color': '#E4E8EC',
        'grid.linewidth': 0.8,
        'axes.axisbelow': True,
        'legend.frameon': False,
        'savefig.dpi': 220,
        'savefig.facecolor': 'white',
    })


def load_votes():
    votes = {}
    for model, *_ in MODELS:
        by_label = {key: defaultdict(Counter) for key, _, _ in LABELS}
        for path in sorted((RESULTS / model / 'provenance/records').glob('*/*.json')):
            record = json.loads(path.read_text(encoding='utf-8'))
            for key in by_label:
                by_label[key][record['paper']['id']][record[key]] += 1
        if any(len(papers) != 20 or any(counts.total() != 10 for counts in papers.values())
               for papers in by_label.values()):
            raise SystemExit(f'{model} 缺少完整的 20 篇 × 10 份原判。')
        votes[model] = by_label
    return votes


def save(figure, name):
    FIGURES.mkdir(parents=True, exist_ok=True)
    figure.savefig(FIGURES / name)
    plt.close(figure)


def label_distribution(votes):
    figure, axes = plt.subplots(1, 2, figsize=(10.5, 3.35), layout='constrained')
    figure.get_layout_engine().set(w_pad=0.16, h_pad=0.12, wspace=0.1)
    for axis, (key, title, categories) in zip(axes, LABELS):
        totals = {model: sum(votes[model][key].values(), Counter()) for model, *_ in MODELS}
        present = [category for category in categories if any(counts[category] for counts in totals.values())]
        positions = list(range(len(present)))
        for shift, (model, name, color, _) in zip((-0.17, 0.17), MODELS):
            values = [totals[model][category] for category in present]
            bars = axis.barh([position + shift for position in positions], values,
                             height=0.28, color=color, label=name)
            axis.bar_label(bars, padding=4, fontsize=9, color=color)
        axis.set_yticks(positions, present)
        axis.set(xlim=(0, 227), ylim=(len(present) - 0.5, -0.5), xlabel='原判份数')
        axis.xaxis.set_major_locator(MultipleLocator(50))
        axis.grid(axis='x')
        axis.tick_params(axis='y', length=0)
        axis.set_title(title, loc='left', pad=10, fontweight='bold')
    handles, names = axes[0].get_legend_handles_labels()
    figure.legend(handles, names, loc='outside upper center', ncols=2)
    save(figure, 'model-label-distribution.png')


def mode_set(counts):
    maximum = max(counts.values())
    return {category for category, count in counts.items() if count == maximum}


def paper_disagreements(votes):
    focus = ('理论+实证', '核心经管')
    figure, axes = plt.subplots(1, 2, figsize=(10.5, 3.6), layout='constrained')
    figure.get_layout_engine().set(w_pad=0.2, h_pad=0.1, wspace=0.11)
    model_one, model_two = (model for model, *_ in MODELS)
    for axis, (key, title, _), category in zip(axes, LABELS, focus):
        left, right = votes[model_one][key], votes[model_two][key]
        if left.keys() != right.keys():
            raise SystemExit('两个模型的论文清单不同。')
        paper_ids = sorted(pid for pid in left if mode_set(left[pid]) != mode_set(right[pid]))
        for position, pid in enumerate(paper_ids):
            values = [votes[model][key][pid][category] for model, *_ in MODELS]
            axis.hlines(position, min(values), max(values), color='#9CA9B2', linewidth=1.6, zorder=2)
            for value, (_, _, color, marker) in zip(values, MODELS):
                axis.scatter(value, position, s=64, marker=marker, color=color,
                             edgecolor='white', linewidth=0.8, zorder=3, clip_on=False)
        axis.set_yticks(range(len(paper_ids)), paper_ids)
        axis.set_xticks(range(0, 11, 2))
        axis.set(xlim=(-0.35, 10.35), ylim=(max(len(paper_ids) - 0.45, 1.55), -0.55),
                 xlabel=f'「{category}」票数（每模型 10 票）', ylabel='论文编号')
        axis.grid(axis='x')
        axis.tick_params(axis='y', length=0, pad=7)
        axis.set_title(f'{title}（{len(paper_ids)}/20 篇）', loc='left', pad=10, fontweight='bold')
    handles = [Line2D([], [], color=color, marker=marker, linestyle='None', markersize=7, label=name)
               for _, name, color, marker in MODELS]
    figure.legend(handles=handles, loc='outside upper center', ncols=2)
    save(figure, 'paper-level-disagreements.png')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', help='Path to a Chinese font file')
    set_style(parser.parse_args().font)
    votes = load_votes()
    label_distribution(votes)
    paper_disagreements(votes)


if __name__ == '__main__':
    main()

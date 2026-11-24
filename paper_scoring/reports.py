from __future__ import annotations

from collections import Counter
from html import escape
from io import BytesIO
from math import ceil
from pathlib import Path
import re
import tempfile
from unicodedata import category, east_asian_width, normalize

import pymupdf
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle
import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

from .judge import LABEL_ONE, LABEL_TWO
from .materials import Paper, Settings, atomic_write

BLUE = "#26435B"
LIGHT = "#F3F6F8"


def md_text(value: str) -> str:
    value = value.replace("\r", " ").replace("\n", " ")
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\|\[\]`*_!])", r"\\\1", value)


def locations(section: dict) -> list[str]:
    names = {"md": "MD 行", "pdf": "PDF 文件页", "pdf_image": "PDF 页图（核对页码）"}
    lines = []
    for item in section["evidence"]:
        start, end = item["start"], item["end"]
        span = str(start) if start == end else f"{start}–{end}"
        reference = f"（{item['ref']}）" if "ref" in item else ""
        quote = " ".join(item["quote"].replace("\u200b", "").replace("\u00ad", "").split())
        quote = "".join("□" if category(char) in {"Cc", "Co"} else normalize("NFKC", char)
                        if "\U0001d400" <= char <= "\U0001d7ff" else char for char in quote).replace("⁎", "*")
        lines.append(f"{names[item['source']]} {span}{reference}：『{quote}』")
    return lines


def details(record: dict) -> list[tuple[str, list[str]]]:
    result = record["judgment"]
    abstract = result["abstract"]
    theory = result["theory"]
    empirical = result["empirical"]
    field = result["field"]
    theory_text = [f"1.1 摘要{'可识别' if abstract['found'] else '在 PDF 中仍无法识别'}。{abstract['reason']}", *locations(abstract)]
    if theory is None:
        theory_text.append("跳过 1.2、1.3，理论未判断。")
        empirical_text = ["因步骤 1.1 无法识别摘要，跳过 2.1、2.2，实证未判断。"]
    else:
        contribution = theory["contribution"]
        proof = theory["derivations"]
        real = empirical["real_data"]
        estimated = empirical["estimated_model"]
        theory_text.extend([f"1.2 {'有' if contribution else '无'}明确的理论贡献表述。",
                            "1.3 跳过，摘要未明确提出理论贡献。" if proof is None else f"1.3 正文{'有' if proof else '无'}数学推导、引理、定理等。",
                            f"结论：{'有理论' if contribution and proof else '无理论'}。{theory['reason']}", *locations(theory)])
        empirical_text = [f"2.1 {'使用' if real else '未使用'}真实数据。",
                          "2.2 跳过，未使用真实数据。" if estimated is None else f"2.2 {'满足' if estimated else '未满足'}明确构建模型并估计出系数。",
                          f"结论：{'有实证' if real and estimated else '无实证'}。{empirical['reason']}", *locations(empirical)]
    names = {"independent": "核心自变量", "dependent": "核心因变量", "central": "最核心变量"}
    variables = [f"{names[v['role']]} {v['name']}（{'经管类' if v['economic'] else '非经管类'}）。{v['reason']}"
                 for v in field["variables"]]
    field_text = [f"路径 {record['field_path']}。{field['reason']}", *variables, *locations(field)]
    sources = {"md": "MD", "pdf": "PDF 文本", "pdf_image": "PDF 页图"}
    source_text = [" → ".join(sources[s] for s in record["modes"]) + f"。PDF 共 {record['pdf_pages']} 页。"]
    if record["image_pages"]:
        source_text.append("回查页图：" + "、".join(map(str, record["image_pages"])) + "。页图短引可按实际页图人工核对。")
    return [("理论判断及依据", theory_text), ("实证判断及依据", empirical_text), ("标签二依据", field_text), ("材料核查", source_text)]


def group_markdown(agent: str, group: str, papers: list[Paper], records: dict[str, dict]) -> str:
    title = f"{agent} {group} 论文双标签详细判定"
    lines = [f"# {title}", "", "| 原始序号 | 题目 | 标签一 | 标签二 |", "|---|---|---|---|"]
    for p in papers:
        if r := records.get(p.id):
            lines.append(f"| {p.id} | {md_text(p.title)} | {r['label_one']} | {r['label_two']} |")
    for p in papers:
        if not (r := records.get(p.id)):
            continue
        lines.extend(["", f"## {p.id} {md_text(p.title)}", "", f"**标签一** {r['label_one']}　**标签二** {r['label_two']}"])
        for heading, content in details(r):
            lines.extend(["", f"### {heading}", "", *(f"- {md_text(t)}" for t in content)])
    return "\n".join(lines) + "\n"


def group_pdf(path: Path, agent: str, group: str, papers: list[Paper], records: dict[str, dict], font_path: str) -> None:
    if "PaperScoring" not in pdfmetrics.getRegisteredFontNames():
        font = font_path or BytesIO(pymupdf.Font("china-s").buffer)
        pdfmetrics.registerFont(TTFont("PaperScoring", font))
    normal = ParagraphStyle("body", fontName="PaperScoring", fontSize=10, leading=15, spaceAfter=6, wordWrap="CJK")
    head = ParagraphStyle("heading", parent=normal, fontSize=19, leading=25, spaceAfter=13, textColor=colors.HexColor(BLUE))
    sub = ParagraphStyle("section", parent=normal, fontSize=11.5, leading=16, spaceBefore=7, textColor=colors.HexColor(BLUE), keepWithNext=True)
    cell = ParagraphStyle("cell", parent=normal, fontSize=9, leading=13, spaceAfter=0)
    cell_center = ParagraphStyle("cell_center", parent=cell, alignment=TA_CENTER)
    header = ParagraphStyle("cell_header", parent=cell_center, textColor=colors.white)

    def p(text: str, style: ParagraphStyle = normal) -> Paragraph:
        return Paragraph(escape(text).replace("\n", "<br/>"), style)

    title = f"{agent} {group} 论文双标签详细判定"
    rows = [[p(t, header) for t in ("原始序号", "题目", "标签一", "标签二")]]
    for paper in papers:
        r = records[paper.id]
        rows.append([p(paper.id, cell_center), p(paper.title, cell), p(r["label_one"], cell_center), p(r["label_two"], cell_center)])
    table = LongTable(rows, colWidths=[21 * mm, 101 * mm, 34 * mm, 22 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(BLUE)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor(LIGHT)]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, -1), (-1, -1), .5, colors.HexColor("#CED8E0")),
    ]))
    flowables = [p(title, head), Spacer(1, 2 * mm), table]
    for paper in papers:
        r = records[paper.id]
        flowables.extend([PageBreak(), p(f"{paper.id}  {paper.title}", head), p(f"标签一  {r['label_one']}        标签二  {r['label_two']}")])
        for heading, content in details(r):
            flowables.append(p(heading, sub))
            flowables.extend(p(line) for line in content)

    def header_footer(canvas, document):
        width, height = A4
        canvas.saveState()
        canvas.setFont("PaperScoring", 9)
        canvas.setFillColor(colors.HexColor(BLUE))
        canvas.drawString(16 * mm, height - 12 * mm, f"{agent}  /  {group}")
        canvas.setStrokeColor(colors.HexColor("#CED8E0"))
        canvas.line(16 * mm, height - 15 * mm, width - 16 * mm, height - 15 * mm)
        canvas.drawRightString(width - 16 * mm, 11 * mm, f"第 {document.page} 页")
        canvas.restoreState()

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as temp:
        pending = Path(temp) / path.name
        doc = SimpleDocTemplate(str(pending), pagesize=A4, title=title,
                                leftMargin=16 * mm, rightMargin=16 * mm, topMargin=21 * mm, bottomMargin=18 * mm)
        doc.build(flowables, onFirstPage=header_footer, onLaterPages=header_footer)
        pending.replace(path)


def agreement(values: list[str], labels: tuple[str, ...], n: int) -> dict:
    votes = Counter(values)
    best = max(votes.values())
    winners = [label for label in labels if votes[label] == best]
    return {"counts": [votes[label] for label in labels], "vote_text": "｜".join(f"{label} {votes[label]} 票" for label in labels),
            "mode": "；".join(f"{label} {best} 票" for label in winners), "rate": best / n,
            "complete": best == n, "review": best != n or votes["无法判断"] > 0,
            "difference": "无" if best == n else "众数并列" if len(winners) > 1 else "存在分歧"}


def detail_row_height(values: list[str]) -> int:
    # Keep room for word wrapping in the 55-character evidence columns.
    lines = [sum(max(1, ceil(sum(2 if east_asian_width(char) in {"W", "F"} else 1 for char in line) / 44))
                 for line in value.splitlines()) for value in values]
    return min(409, max(95, 15 * max(lines) + 8))


def workbook(path: Path, papers: tuple[Paper, ...], agents: list[str], by_agent: dict[str, dict[str, dict]]) -> None:
    n = len(agents)
    with tempfile.TemporaryDirectory(dir=path.parent) as temp:
        pending = Path(temp) / path.name
        with xlsxwriter.Workbook(pending, {"strings_to_formulas": False, "strings_to_urls": False}) as book:
            sheets = [book.add_worksheet(title) for title in ("标签一汇总", "标签二汇总", "各Agent结果")]
            header = book.add_format({"font_name": "Microsoft YaHei", "bold": True, "font_color": "white", "bg_color": BLUE, "text_wrap": True, "valign": "vcenter"})
            title_format = book.add_format({"font_name": "Microsoft YaHei", "bold": True, "font_size": 16, "font_color": BLUE, "valign": "vcenter"})
            note_format = book.add_format({"font_name": "Microsoft YaHei", "font_color": "#415A70", "valign": "vcenter"})
            style = {"font_name": "Microsoft YaHei", "font_size": 10, "text_wrap": True, "valign": "vcenter"}
            body = [book.add_format(style | {"bg_color": color}) for color in ("#FFFFFF", LIGHT)]
            pct = [book.add_format(style | {"bg_color": color, "num_format": "0%"}) for color in ("#FFFFFF", LIGHT)]
            link = [book.add_format(style | {"bg_color": color, "font_color": "#145886", "underline": 1}) for color in ("#FFFFFF", LIGHT)]
            manual = book.add_format(style | {"bg_color": "#FFF1BA"})

            def setup(sheet, columns, widths, count, explanation):
                sheet.merge_range(0, 0, 0, len(columns) - 1, sheet.name, title_format)
                sheet.merge_range(1, 0, 1, len(columns) - 1, explanation, note_format)
                sheet.write_row(2, 0, columns, header)
                for col, width in enumerate(widths):
                    sheet.set_column(col, col, width)
                sheet.set_row(0, 29)
                sheet.set_row(1, 23)
                sheet.set_row(2, 27)
                sheet.freeze_panes(3, 2)
                sheet.autofilter(2, 0, count + 2, len(columns) - 1)
                sheet.hide_gridlines(2)

            detail = sheets[2]
            detail_headers = ["清单位置", "原始序号", "题目", "Agent编号", "组号", "标签一", "标签二", "理论判断及依据", "实证判断及依据", "标签二依据", "Markdown明细文件", "PDF明细文件"]
            setup(detail, detail_headers, [12, 12, 43, 15, 10, 23, 13, 55, 55, 55, 31, 31], n * len(papers), "按 Agent 编号、组号、论文顺序排列。明细文件链接相对于本工作簿。")
            for index, (agent, paper) in enumerate((a, p) for a in agents for p in papers):
                row = index + 3
                fmt = body[index % 2]
                r = by_agent[agent][paper.id]
                detail_values = ["\n".join(lines) for _, lines in details(r)[:3]]
                detail.write_row(row, 0, [paper.position, paper.id, paper.title, agent, paper.group, r["label_one"], r["label_two"], *detail_values], fmt)
                detail.set_row(row, detail_row_height(detail_values))
                for col, folder, extension in ((10, "MD-每组详细判定", "md"), (11, "PDF-每组详细判定", "pdf")):
                    name = f"{agent}-{paper.group}.{extension}"
                    detail.write_url(row, col, f"external:{folder}/{name}", link[index % 2], name)

            summary_headers = ["清单位置", "原始序号", "题目", "10次结果汇总" if n == 10 else f"{n}次结果汇总", "众数标签和次数", "一致率", "是否完全一致", "是否需人工复核", "不一致类型", "人工结果"]
            for sheet, labels, key, col in zip(sheets, (LABEL_ONE, LABEL_TWO), ("label_one", "label_two"), ("F", "G")):
                setup(sheet, summary_headers, [12, 12, 44, 51, 32, 12, 16, 17, 15, 18], len(papers),
                      f"每篇 {n} 份独立结果。一致率＝最高票数 ÷ {n}。并列众数逐项列出。")
                end = xl_col_to_name(9 + len(labels))
                sheet.set_column(f"K:{end}", 14, None, {"hidden": True})
                for j, label in enumerate(labels):
                    sheet.write_string(2, 10 + j, label, header)
                for index, paper in enumerate(papers):
                    r, row = index + 3, index + 4
                    fmt = body[index % 2]
                    stats = agreement([by_agent[agent][paper.id][key] for agent in agents], labels, n)
                    sheet.write_row(r, 0, [paper.position, paper.id, paper.title], fmt)
                    sheet.set_row(r, 52)
                    refs = [f"{xl_col_to_name(10 + j)}{row}" for j in range(len(labels))]
                    maximum = f"MAX(K{row}:{end}{row})"
                    scope = f"K{row}:{end}{row}"
                    last = n * len(papers) + 3
                    for j, ref in enumerate(refs):
                        top = xl_col_to_name(10 + j) + "$3"
                        count = f"=COUNTIFS('各Agent结果'!$B$4:$B${last},$B{row},'各Agent结果'!${col}$4:${col}${last},{top})"
                        sheet.write_formula(r, 10 + j, count, fmt, stats["counts"][j])
                    votes = '&"｜"&'.join(f'{xl_col_to_name(10 + j)}$3&" "&{ref}&" 票"' for j, ref in enumerate(refs))
                    modes = []
                    for j, ref in enumerate(refs):
                        separator = f'IF(COUNTIF(K{row}:{refs[j - 1]},{maximum})>0,"；","")&' if j else ""
                        modes.append(f'IF({ref}={maximum},{separator}{xl_col_to_name(10 + j)}$3&" "&{ref}&" 票","")')
                    sheet.write_formula(r, 3, "=" + votes, fmt, stats["vote_text"])
                    sheet.write_formula(r, 4, "=" + "&".join(modes), fmt, stats["mode"])
                    sheet.write_formula(r, 5, f"={maximum}/{n}", pct[index % 2], stats["rate"])
                    sheet.write_formula(r, 6, f'=IF({maximum}={n},"是","否")', fmt, "是" if stats["complete"] else "否")
                    unknown = f",{refs[-1]}>0" if labels == LABEL_ONE else ""
                    sheet.write_formula(r, 7, f'=IF(OR({maximum}<{n}{unknown}),"是","否")', fmt, "是" if stats["review"] else "否")
                    sheet.write_formula(r, 8, f'=IF({maximum}={n},"无",IF(COUNTIF({scope},{maximum})>1,"众数并列","存在分歧"))', fmt, stats["difference"])
                    sheet.write_blank(r, 9, None, manual)
        pending.replace(path)


def review_markdown(papers: tuple[Paper, ...], agents: list[str], by_agent: dict[str, dict[str, dict]]) -> str:
    lines = ["# 不一致论文复核", ""]
    for paper in papers:
        results = [by_agent[a][paper.id] for a in agents]
        one, two = {r["label_one"] for r in results}, {r["label_two"] for r in results}
        reasons = (["存在分歧"] if len(one) > 1 or len(two) > 1 else []) + (["存在无法判断结果"] if "无法判断" in one else [])
        if not reasons:
            continue
        lines.extend([f"## {paper.id} {md_text(paper.title)}", "", "复核原因  " + "、".join(reasons), ""])
        for agent, record in zip(agents, results):
            lines.append(f"- **{agent}**　{record['label_one']} / {record['label_two']}")
            for heading, content in details(record)[:3]:
                lines.append(f"  - {heading}　" + "；".join(md_text(x) for x in content))
        lines.append("")
    if len(lines) == 2:
        lines.append("本轮没有需人工复核的论文。")
    return "\n".join(lines) + "\n"


def acceptance(papers: tuple[Paper, ...], agents: list[str], by_agent: dict[str, dict[str, dict]], sessions: dict,
               model: dict, concurrency: int, run_id: str, smoke: bool, complete: bool, failed: list[str]) -> str:
    counts = {p.id: sum(p.id in by_agent[a] for a in agents) for p in papers}
    lines = ["# 验收报告", "", f"- 运行标识：{run_id}", f"- 模型：{md_text(model['model'])}",
             f"- 子 Agent 框架：{md_text(model['agent_framework'])}",
             f"- 运行模式：{'有费用的单篇试跑' if smoke else '正式实验'}",
             f"- 实测子 Agent 最大并发：{concurrency}（按有开始及结束时间的进程区间计算）", f"- 有效记录数：{sum(counts.values())} / {len(papers) * len(agents)}",
             f"- 验收状态：{'十份结果齐全' if complete and not smoke else '试跑齐全' if complete else '待补齐'}",
             "- 隔离方式：每个子 Agent 在独立本机进程中顺序处理论文。每次 Pydantic AI 运行独立创建请求上下文，发送共同规则和当前论文材料。若校验失败，重试提示只含本轮校验问题。",
             "- 会话标识：下表的 UUID 由本程序生成，用于追踪运行及恢复。第三方网关内部的路由、缓存和上下文不可由本程序审计。", "",
             "## Agent 完成情况", "", "| Agent | 完成篇数 | 本地会话标识和状态 |", "|---|---:|---|"]
    for agent in agents:
        history = sessions.get(agent, [])
        statuses = "；".join(f"`{h['session_id']}`（{h['status']}，{'恢复' if h['resumed'] else '首次'}，PID {h['pid']}）" for h in history)
        lines.append(f"| {agent} | {len(by_agent[agent])} / {len(papers)} | {statuses or '尚未启动'} |")
    format_retries = [(agent, h) for agent in agents for h in sessions.get(agent, []) if h.get("retry_format")]
    if format_retries:
        lines.extend(["", "## 格式校验恢复", ""])
        for agent, session in format_retries:
            retry = session["retry_format"]
            lines.append(f"- {agent}，`{session['session_id']}`，论文 {'、'.join(retry['papers'])}。{md_text(retry['instructions'])}")
    lines.extend(["", "## 每篇有效判读次数", "", "| 论文 | 有效判读 |", "|---|---:|"])
    lines.extend(f"| {p.id} | {counts[p.id]} / {len(agents)} |" for p in papers)
    if complete:
        items = [[(by_agent[a][p.id]["label_one"], by_agent[a][p.id]["label_two"]) for a in agents] for p in papers]
        one = sum(len({a for a, _ in pairs}) == 1 for pairs in items)
        two = sum(len({b for _, b in pairs}) == 1 for pairs in items)
        both = sum(len(set(pairs)) == 1 for pairs in items)
        lines.extend(["", "## 完全一致的篇数", "", f"- 标签一：{one} / {len(papers)}", f"- 标签二：{two} / {len(papers)}", f"- 标签组合：{both} / {len(papers)}"])
    else:
        lines.extend(["", "尚未收齐结果。本次未生成一致率、Excel 或最终复核清单。"])
    if failed:
        lines.extend(["", "## 待处理异常", "", *(f"- {md_text(issue)}" for issue in failed)])
    return "\n".join(lines) + "\n"


def export_final(directory: Path, papers: tuple[Paper, ...], agents: list[str], by_agent: dict[str, dict[str, dict]], settings: Settings) -> None:
    groups = dict.fromkeys(p.group for p in papers)
    for agent in agents:
        for group in groups:
            subset = [p for p in papers if p.group == group]
            records = by_agent[agent]
            stem = f"{agent}-{group}"
            md_path = directory / "MD-每组详细判定" / f"{stem}.md"
            pdf_path = directory / "PDF-每组详细判定" / f"{stem}.pdf"
            if not md_path.is_file():
                atomic_write(md_path, group_markdown(agent, group, subset, records))
            if not pdf_path.is_file():
                group_pdf(pdf_path, agent, group, subset, records, settings.pdf_font)
    xlsx = directory / f"标签一二-{len(agents)}个Agent独立打标汇总.xlsx"
    if not xlsx.is_file():
        workbook(xlsx, papers, agents, by_agent)
    review = directory / "不一致论文复核.md"
    if not review.is_file():
        atomic_write(review, review_markdown(papers, agents, by_agent))

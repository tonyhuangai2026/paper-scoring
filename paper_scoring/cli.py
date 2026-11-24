from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from itertools import groupby
import json
import multiprocessing
import os
from pathlib import Path
import sys
from uuid import uuid4

from filelock import FileLock, Timeout
import pydantic_ai

from .judge import FatalAPIError, Gateway, RUBRIC, classify, ensure, open_paper, probe, score
from .materials import Manifest, Paper, ScoringError, Settings, atomic_write, load_manifest, sha256, write_json
from .reports import acceptance, export_final, group_markdown, md_text

WORK = "01_Agent工作区"
FINAL = "04_汇总结果"
THEORY_FORMAT_REMINDER = (
    '复核 JSON 字段的条件格式。若 theory.contribution 为 false，theory.derivations 必须为 JSON null，'
    '即使正文有数学公式也沿此分支跳过步骤 1.3。若 theory.contribution 为 true，'
    'theory.derivations 必须为 JSON 布尔值。独立按原规则和原文判断 theory.contribution。'
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScoringError(f"无法读取本地记录 {path.name}。") from exc
    if not isinstance(data, dict):
        raise ScoringError(f"本地记录 {path.name} 格式错误。")
    return data


def record_path(agent_dir: Path, paper: Paper) -> Path:
    return agent_dir / "records" / f"{paper.id}.json"


def read_record(agent_dir: Path, agent: str, paper: Paper, run_id: str) -> dict:
    data = read_json(record_path(agent_dir, paper))
    ensure(data.get("run_id") == run_id and data.get("agent") == agent and data.get("paper") == paper.snapshot(), "原判与本轮清单或 Agent 不符。")
    modes, images, pages = data.get("modes"), data.get("image_pages"), data.get("pdf_pages")
    ensure(modes in (["md"], ["pdf"], ["md", "pdf"], ["pdf", "pdf_image"], ["md", "pdf", "pdf_image"]), "原判材料核查记录无效。")
    ensure(type(pages) is int and pages > 0 and isinstance(images, list)
           and all(type(i) is int and 1 <= i <= pages for i in images)
           and bool(images) == (modes[-1] == "pdf_image"), "原判 PDF 核查页码无效。")
    ensure(isinstance(data.get("judgment"), dict), "原判缺少判断内容。")
    expected = classify(data["judgment"], modes[-1])
    ensure(expected == (data.get("label_one"), data.get("label_two"), data.get("field_path")), "原判标签与规则不符。")
    for name in ("abstract", "theory", "empirical", "field"):
        section = data["judgment"][name]
        if section is None:
            continue
        for evidence in section["evidence"]:
            source = evidence["source"]
            ensure((source == "md" and modes[-1] == "md") or (source == "pdf" and modes[-1] != "md" and evidence["end"] <= pages)
                   or (source == "pdf_image" and set(range(evidence["start"], evidence["end"] + 1)).issubset(images)), "原判引用了超出材料范围的证据。")
    return data


def existing_records(agent_dir: Path, agent: str, papers: tuple[Paper, ...], run_id: str) -> dict[str, dict]:
    records = {}
    for paper in papers:
        if not record_path(agent_dir, paper).is_file():
            continue
        try:
            records[paper.id] = read_record(agent_dir, agent, paper, run_id)
        except ScoringError:
            # An invalid local record is never counted. Its original worker will retry the paper.
            continue
    return records


def agent_progress(agent_dir: Path, agent: str, papers: tuple[Paper, ...], records: dict, failures: dict) -> None:
    next_paper = next((p.id for p in papers if p.id not in records), "全部完成")
    lines = [f"# {agent} 进度", "", f"完成 {len(records)} / {len(papers)} 篇。", f"最早待处理：{next_paper}", ""]
    for group, subset in groupby(papers, key=lambda paper: paper.group):
        done = [p.id for p in subset if p.id in records]
        lines.append(f"- {group} 已完成：{'、'.join(done) or '无'}")
    if failures:
        lines.extend(["", "## 待处理问题", "", *(f"- {pid}：{md_text(reason)}" for pid, reason in failures.items())])
    atomic_write(agent_dir / "进度.md", "\n".join(lines) + "\n")
    write_json(agent_dir / "failures.json", failures)


def agent_history(agent_dir: Path) -> list[dict]:
    path = agent_dir / "sessions.json"
    return read_json(path).get("sessions", []) if path.is_file() else []


def run_agent(agent: str, agent_dir: Path, papers: tuple[Paper, ...], settings: Settings, run_id: str) -> bool:
    pydantic_ai.BANNER_ENABLED = False
    records = existing_records(agent_dir, agent, papers, run_id)
    failures_path = agent_dir / "failures.json"
    failures = read_json(failures_path) if failures_path.is_file() else {}
    history = agent_history(agent_dir)
    for previous in history:
        if previous["status"] == "运行中":
            previous["status"] = "中断（结束时间未知）"
    session = {"session_id": str(uuid4()), "pid": os.getpid(), "started_at": now(), "status": "运行中", "resumed": bool(history)}
    retry_theory = {paper_id for paper_id, reason in failures.items()
                    if paper_id not in records and reason == "理论步骤与摘要判断不匹配。"}
    if retry_theory:
        session["retry_format"] = {"papers": sorted(retry_theory), "instructions": THEORY_FORMAT_REMINDER}
    history.append(session)
    write_json(agent_dir / "sessions.json", {"sessions": history})
    fatal = False
    try:
        with Gateway(settings, agent) as gateway:
            format_reminder = None

            @gateway.agent.instructions
            def prior_format_check() -> str | None:
                return format_reminder

            for group, subset in groupby(papers, key=lambda p: p.group):
                group_papers = list(subset)
                group_file = agent_dir / f"{group}.md"
                atomic_write(group_file, group_markdown(agent, group, group_papers, records))
                for paper in group_papers:
                    if paper.id in records:
                        continue
                    format_reminder = THEORY_FORMAT_REMINDER if paper.id in retry_theory else None
                    try:
                        verdict = score(paper, gateway)
                    except ScoringError as exc:
                        failures[paper.id] = str(exc)
                        fatal = isinstance(exc, FatalAPIError)
                    else:
                        record = {"agent": agent, "run_id": run_id, "session_id": session["session_id"],
                                  "finished_at": now(), "paper": paper.snapshot(), **verdict}
                        write_json(record_path(agent_dir, paper), record)
                        records[paper.id] = record
                        failures.pop(paper.id, None)
                        atomic_write(group_file, group_markdown(agent, group, group_papers, records))
                    agent_progress(agent_dir, agent, papers, records, failures)
                    if fatal:
                        return True
    finally:
        session.update(ended_at=now(), status="完成" if len(records) == len(papers) else "待补齐")
        write_json(agent_dir / "sessions.json", {"sessions": history})
    return fatal


def write_assignment(root: Path, manifest: Manifest, papers: tuple[Paper, ...], settings: Settings, smoke: bool) -> str:
    assignment = root / "00_任务清单"
    plan_file = assignment / "run.json"
    code = Path(__file__).with_name("judge.py").read_bytes()
    plan = {"name": manifest.name, "papers": [paper.snapshot() for paper in papers], "smoke": smoke,
            "instructions_sha256": manifest.instructions_sha256, "rubric_sha256": sha256(RUBRIC.encode()),
            "judge_sha256": sha256(code), "model": settings.model_config()}
    if plan_file.exists():
        current = read_json(plan_file)
        previous = current.get("plan", {})
        prior_papers = previous.get("papers", [])
        same_config = {k: v for k, v in previous.items() if k != "papers"} == {k: v for k, v in plan.items() if k != "papers"}
        same_papers = len(prior_papers) == len(papers) and all(
            old["id"] == new["id"] and (not old["ready"] or old == new)
            for old, new in zip(prior_papers, plan["papers"])
        )
        if not same_config or not same_papers:
            raise ScoringError("当前清单、模型或判定规则与此运行目录不匹配。请为改变后的实验指定新的 --run-dir。")
        run_id = current["run_id"]
        if previous != plan:
            write_json(plan_file, {"run_id": run_id, "plan": plan})
    else:
        run_id = str(uuid4())
        write_json(plan_file, {"run_id": run_id, "plan": plan})
    lines = [f"# {md_text(manifest.name)} 分组清单", "", f"运行标识 `{run_id}`", ""]
    if manifest.instructions:
        lines += [f"正式指令 `{manifest.instructions}`", ""]
    lines += ["| 组号 | 清单位置 | 论文编号 | PDF 路径 | MD 路径 |", "|---|---:|---|---|---|"]
    lines += [f"| {p.group} | {p.position} | {p.id} | {md_text(str(p.pdf)) if p.pdf else '待补齐'} | {md_text(str(p.md)) if p.md else '待补齐'} |" for p in papers]
    atomic_write(assignment / "分组清单.md", "\n".join(lines) + "\n")
    return run_id


def schedule(agents: list[str], root: Path, papers: tuple[Paper, ...], settings: Settings, run_id: str, jobs: int) -> list[str]:
    waiting = iter(a for a in agents if len(existing_records(root / WORK / a, a, papers, run_id)) < len(papers))
    initial = [a for _, a in zip(range(jobs), waiting)]
    if not initial:
        return []
    issues = []
    stop = False
    with ProcessPoolExecutor(max_workers=jobs, mp_context=multiprocessing.get_context("spawn"), max_tasks_per_child=1) as pool:
        def submit(agent):
            print(f"启动 {agent}", flush=True)
            return pool.submit(run_agent, agent, root / WORK / agent, papers, settings, run_id)

        active = {submit(a): a for a in initial}
        while active:
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                agent = active.pop(future)
                try:
                    if future.result():
                        stop = True
                except Exception as exc:
                    issues.append(f"{agent} 的本机进程异常退出（{type(exc).__name__}），已完成的记录可恢复。")
                    stop = True
                count = len(existing_records(root / WORK / agent, agent, papers, run_id))
                print(f"{agent} 有效结果 {count}/{len(papers)}", flush=True)
            if not stop:
                for _ in done:
                    if next_agent := next(waiting, None):
                        active[submit(next_agent)] = next_agent
    if stop:
        issues.append("检测到共享 API 配置或进程问题，已停止启动剩余 Agent。")
    return issues


def max_concurrency(sessions: dict[str, list[dict]]) -> int:
    events = []
    for history in sessions.values():
        for item in history:
            if ended := item.get("ended_at"):
                events.extend([(item["started_at"], 1), (ended, -1)])
    peak = active = 0
    for _, delta in sorted(events):
        active += delta
        peak = max(peak, active)
    return peak


def run_experiment(manifest: Manifest, settings: Settings, root: Path, jobs: int, smoke: bool) -> bool:
    papers = manifest.papers[:1] if smoke else manifest.papers
    agents = [f"agent_{n:02d}" for n in range(1, 2 if smoke else 11)]
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / ".run.lock", timeout=0):
        run_id = write_assignment(root, manifest, papers, settings, smoke)
        failures = schedule(agents, root, papers, settings, run_id, min(jobs, len(agents)))
        by_agent, sessions = {}, {}
        for agent in agents:
            agent_dir = root / WORK / agent
            by_agent[agent] = existing_records(agent_dir, agent, papers, run_id)
            sessions[agent] = agent_history(agent_dir)
            path = agent_dir / "failures.json"
            if path.is_file():
                failures.extend(f"{agent} / {pid}：{reason}" for pid, reason in read_json(path).items()
                                if pid not in by_agent[agent])
        complete = all(len(records) == len(papers) for records in by_agent.values())
        final = root / FINAL
        final.mkdir(parents=True, exist_ok=True)
        if complete:
            export_state = root / "00_任务清单" / "export.json"
            result_hash = sha256(json.dumps(by_agent, ensure_ascii=False, sort_keys=True).encode())
            if export_state.exists() and read_json(export_state).get("result_sha256") != result_hash:
                raise ScoringError("已有汇总对应另一版原判。请保留之前的汇总文件，并移出 04_汇总结果 与 00_任务清单/export.json 后重新汇总。")
            if not export_state.exists():
                write_json(export_state, {"result_sha256": result_hash})
            export_final(final, papers, agents, by_agent, settings)
        atomic_write(final / "验收报告.md", acceptance(papers, agents, by_agent, sessions, settings.model_config(),
                                                     max_concurrency(sessions), run_id, smoke, complete, failures))
        print(f"{'完成' if complete else '待补齐'}：{final}", flush=True)
        return complete


def default_run_dir(manifest_path: Path, manifest: Manifest, smoke: bool) -> Path:
    name = "smoke" if smoke else manifest.name + "-标签一二"
    safe_name = name.replace("/", "_").replace("\\", "_").replace("\x00", "_")
    return manifest_path.parent / "runs" / safe_name


def main(argv=None) -> int:
    pydantic_ai.BANNER_ENABLED = False
    parser = argparse.ArgumentParser(description="独立论文双标签实验")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="核对清单与本地材料，不调用模型")
    doctor = commands.add_parser("doctor", help="用合成数据检查网关和模型，可能产生少量费用")
    run = commands.add_parser("run", help="正式运行或断点续跑，论文内容会提交给配置的网关")
    for command in (check, run):
        command.add_argument("--manifest", type=Path, default=Path("papers.toml"))
    for command in (doctor, run):
        command.add_argument("--env-file", type=Path, default=Path(".env"))
    run.add_argument("--run-dir", type=Path)
    run.add_argument("--jobs", type=int, choices=(1, 2, 3), default=3)
    run.add_argument("--smoke", action="store_true", help="独立试跑，仅 1 个 Agent 和清单首篇")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            for check_result in probe(Settings.read(args.env_file)):
                print(check_result)
            return 0
        manifest_path = args.manifest.resolve()
        manifest = load_manifest(manifest_path)
        if args.command == "check":
            issues = []
            for paper in manifest.papers:
                try:
                    with open_paper(paper):
                        print(f"{paper.group}  {paper.id}  {paper.title}")
                except ScoringError as exc:
                    issues.append(f"{paper.id}：{exc}")
            if issues:
                raise ScoringError("材料核查失败：\n" + "\n".join(issues))
            print(f"共 {len(manifest.papers)} 篇论文，PDF 与 MD 一一对应。")
            return 0
        settings = Settings.read(args.env_file)
        root = args.run_dir or default_run_dir(manifest_path, manifest, args.smoke)
        return 0 if run_experiment(manifest, settings, root.resolve(), args.jobs, args.smoke) else 2
    except Timeout:
        print("当前运行目录已被另一进程占用。", file=sys.stderr)
        return 1
    except (ScoringError, OSError, ValueError) as exc:
        message = type(exc).__name__ if not isinstance(exc, ScoringError) else str(exc)
        print(f"运行失败：{message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("运行中断，已保存的单篇结果可续跑。", file=sys.stderr)
        return 130

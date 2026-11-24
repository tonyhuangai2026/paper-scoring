from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import re
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from openai import APIConnectionError, APIStatusError, AsyncOpenAI, DefaultAsyncHttpxClient
import pymupdf
from pydantic import RootModel, ValidationError
from pydantic_ai import Agent, BinaryContent, PromptedOutput, UsageLimits
from pydantic_ai.exceptions import ModelAPIError, ModelConnectionError, ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai_compatible import OpenAICompatibleProvider

from .materials import Paper, ScoringError, Settings, sha256

RUBRIC = Path(__file__).with_name("rubric.md").read_text(encoding="utf-8")
LABEL_ONE = ("理论", "实证", "理论+实证", "描述性统计和综述", "无法判断")
LABEL_TWO = ("核心经管", "广义经管")
ABSTRACT_MARKER = re.compile(r"^\s*[>#*_|:：.\-\[\] ]*(?:abstract\b|摘要|内容提要)", re.IGNORECASE | re.MULTILINE)


class InvalidResult(ScoringError):
    """The model or a stored record does not follow the scoring contract."""


class FatalAPIError(ScoringError):
    """A gateway configuration or authentication issue shared by all agents."""


Content = str | list[str | BinaryContent]
T = TypeVar("T")


class Gateway:
    def __init__(self, settings: Settings, agent_name: str):
        self.settings = settings
        self.runner = asyncio.Runner()
        self.client = AsyncOpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=settings.timeout,
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(follow_redirects=False),
        )
        provider = OpenAICompatibleProvider("paper-scoring-gateway", openai_client=self.client)
        model = OpenAIChatModel(settings.model, provider=provider, profile=OpenAIModelProfile(
            supports_json_object_output=True, openai_chat_supports_max_completion_tokens=False,
        ))
        self.agent = Agent(
            model,
            name=agent_name,
            output_type=PromptedOutput(RootModel[dict[str, Any]], template=False),
            retries=0,
            model_settings={
                "temperature": 0,
                "max_tokens": settings.max_output_tokens,
                "max_retries": 0,
                "extra_body": {"thinking": {"type": "disabled"}, "reasoning_effort": "none"},
            },
        )

    def __enter__(self) -> Gateway:
        return self

    def __exit__(self, *args) -> None:
        try:
            self.runner.run(self.client.close())
        finally:
            self.runner.close()

    def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        for attempt in range(3):
            pause = 2 ** attempt
            try:
                return self.runner.run(operation())
            except (ModelConnectionError, APIConnectionError):
                if attempt == 2:
                    raise ScoringError("模型 API 连接失败或超时，已重试。") from None
            except (ModelHTTPError, ModelAPIError, APIStatusError) as exc:
                status = exc if isinstance(exc, (ModelHTTPError, APIStatusError)) else exc.__cause__
                if not isinstance(status, (ModelHTTPError, APIStatusError)):
                    raise InvalidResult("API 响应无法解析。") from None
                code = status.status_code
                if code in {401, 402, 403, 404, 405} or 300 <= code < 400:
                    raise FatalAPIError(f"模型 API HTTP {code}，请检查网关地址、鉴权、余额及模型权限。") from None
                if code not in {408, 425, 429} and code < 500:
                    raise ScoringError(f"模型 API HTTP {code}，网关拒绝了本次请求。") from None
                if attempt == 2:
                    raise ScoringError(f"模型 API HTTP {code}，已重试。") from None
                retry_after = (status.retry_after if isinstance(status, ModelHTTPError)
                               else status.response.headers.get("Retry-After", ""))
                if retry_after is not None:
                    try:
                        pause = min(30, max(pause, float(retry_after)))
                    except ValueError:
                        pass
            time.sleep(pause)
        raise AssertionError("unreachable")

    def list_model_ids(self) -> list[str]:
        try:
            return [model.id for model in self.call(self.client.models.list).data]
        except (AttributeError, TypeError, ValidationError):
            raise ScoringError("API 模型列表格式不正确。") from None

    def complete(self, content: Content, *, instructions: str | None = None) -> dict:
        async def run():
            return await self.agent.run(
                content, instructions=instructions, usage_limits=UsageLimits(request_limit=1),
            )

        try:
            result = self.call(run)
        except (UnexpectedModelBehavior, UsageLimitExceeded, ValidationError):
            raise InvalidResult("模型未返回有效的 JSON 对象，或回复被截断。") from None
        if result.response.finish_reason in {"length", "content_filter", "error"}:
            raise InvalidResult("模型回复被截断或未完整生成。")
        return result.output.root


def image_content(png: bytes) -> BinaryContent:
    return BinaryContent(data=png, media_type="image/png")


def probe(settings: Settings) -> list[str]:
    with Gateway(settings, "doctor") as gateway:
        if settings.model not in gateway.list_model_ids():
            raise ScoringError(f"网关模型列表中没有 {settings.model}。")
        if gateway.complete('只返回 JSON：{"ok":true}').get("ok") is not True:
            raise ScoringError("模型未通过 JSON 测试。")
        checks = [f"Pydantic AI 已确认模型 {settings.model} 和 JSON 响应。"]
        if settings.pdf_images:
            with pymupdf.open() as document:
                page = document.new_page(width=180, height=70)
                page.insert_text((34, 48), "7392", fontsize=36)
                png = page.get_pixmap(alpha=False).tobytes("png")
            content = ['看图片，返回 JSON 对象，例如 {"digits":"1234"}。填写图片中的四个数字。', image_content(png)]
            value = gateway.complete(content)
            if str(value.get("digits")) != "7392":
                raise ScoringError("模型未通过页图识别测试。")
            checks.append("已通过 PDF 页图输入测试。")
    return checks


@contextmanager
def open_paper(paper: Paper):
    if paper.issue or paper.md is None or paper.pdf is None:
        raise ScoringError(paper.issue or "PDF 或 MD 无法打开。")
    try:
        md_bytes, pdf_bytes = paper.md.read_bytes(), paper.pdf.read_bytes()
        if sha256(md_bytes) != paper.md_sha256 or sha256(pdf_bytes) != paper.pdf_sha256:
            raise ScoringError("材料内容在运行中发生变化。")
        md = md_bytes.decode("utf-8-sig")
        pdf = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ScoringError("PDF 或 MD 无法打开。") from exc
    with pdf:
        if not pdf.is_pdf or pdf.needs_pass or not pdf.page_count:
            raise ScoringError("PDF 加密、无页或格式无效。")
        yield md, pdf


def ensure(condition: bool, description: str) -> None:
    if not condition:
        raise InvalidResult(description)


def valid_text(value: object, limit: int = 3000) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def evidence_shape(evidence: object, required: bool) -> None:
    ensure(isinstance(evidence, list) and len(evidence) <= 16 and (not required or bool(evidence)), "缺少证据或证据项过多。")
    for item in evidence:
        ensure(isinstance(item, dict), "证据项必须为对象。")
        start, end = item.get("start"), item.get("end")
        ensure(item.get("source") in ("md", "pdf", "pdf_image"), "证据 source 不合法。")
        ensure(type(start) is int and type(end) is int and 1 <= start <= end <= start + 7, "证据范围不合法。")
        ensure(valid_text(item.get("quote"), 360), "证据短引缺失或过长。")


def section(result: dict, name: str, evidence_required: bool = True) -> dict:
    data = result.get(name)
    ensure(isinstance(data, dict) and valid_text(data.get("reason")), f"缺少 {name} 判断理由。")
    evidence_shape(data.get("evidence"), evidence_required)
    return data


def classify(result: dict, mode: str) -> tuple[str, str, str]:
    ensure(result.get("status") == "ok", "只有完成状态可生成标签。")
    ensure(all(name in result for name in ("abstract", "theory", "empirical", "field")), "判定结构缺少必要字段。")
    abstract = section(result, "abstract")
    found = abstract.get("found")
    ensure(type(found) is bool, "abstract.found 必须为布尔值。")
    if not found:
        ensure(mode != "md", "找不到摘要时必须核查 PDF。")
        ensure(result["theory"] is None and result["empirical"] is None, "无摘要时必须跳过理论和实证。")
        label_one = "无法判断"
        path = "3.3"
    else:
        theory = section(result, "theory")
        empirical = section(result, "empirical")
        contribution, derivations = theory.get("contribution"), theory.get("derivations")
        real_data, estimated = empirical.get("real_data"), empirical.get("estimated_model")
        ensure(type(contribution) is bool and "derivations" in theory
               and (type(derivations) is bool if contribution else derivations is None), "理论步骤与摘要判断不匹配。")
        ensure(type(real_data) is bool and "estimated_model" in empirical
               and (type(estimated) is bool if real_data else estimated is None), "实证步骤与真实数据判断不匹配。")
        has_theory = bool(contribution and derivations)
        has_empirical = bool(real_data and estimated)
        label_one = {(True, False): "理论", (False, True): "实证", (True, True): "理论+实证",
                     (False, False): "描述性统计和综述"}[has_theory, has_empirical]
        path = "3.2" if has_empirical else "3.1" if has_theory else "3.3"
    field = section(result, "field", evidence_required=path != "3.1")
    variables = field.get("variables")
    ensure(isinstance(variables, list) and len(variables) <= 16, "核心变量必须为列表。")
    roles = set()
    for variable in variables:
        ensure(isinstance(variable, dict) and variable.get("role") in ("central", "independent", "dependent")
               and valid_text(variable.get("name"), 400) and type(variable.get("economic")) is bool
               and valid_text(variable.get("reason"), 1200), "核心变量缺少名称、角色或经管属性依据。")
        roles.add(variable["role"])
    ensure((path == "3.1" and not variables) or (path == "3.2" and roles == {"independent", "dependent"})
           or (path == "3.3" and len(variables) == 1 and roles == {"central"}), "核心变量与标签二判断路径不匹配。")
    label_two = "核心经管" if path == "3.1" or any(v["economic"] for v in variables) else "广义经管"
    return label_one, label_two, path


def normalized_quote(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\u00ad", "").replace("\u200b", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    return re.sub(r"[\s*_`]+", "", text)


def check_evidence(result: dict, mode: str, md_lines: list[str], pdf_pages: list[str], image_pages: list[int]) -> None:
    sources = {"md": md_lines} if mode == "md" else {"pdf": pdf_pages}
    for name in ("abstract", "theory", "empirical", "field"):
        data = result[name]
        if data is None:
            continue
        for item in data["evidence"]:
            source, start, end = item["source"], item["start"], item["end"]
            if source == "pdf_image":
                ensure(mode == "pdf_image" and set(range(start, end + 1)).issubset(image_pages), "页图证据引用了未提供的页。")
                continue
            ensure(source in sources and end <= len(sources[source]), "证据来源或范围不在本次提供的材料内。")
            passage = "\n".join(sources[source][start - 1:end])
            quote = normalized_quote(item["quote"])
            ensure(bool(quote) and (item["quote"] in passage or quote in normalized_quote(passage)),
                   f"{name} 短引无法在所报 {source} 范围 {start}-{end} 定位。")


def page_images(pdf: pymupdf.Document, pages: list[int]) -> list[str | BinaryContent]:
    parts = []
    try:
        for number in pages:
            page = pdf[number - 1]
            zoom = min(2.2, 1800 / max(page.rect.width, page.rect.height))
            png = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
            parts.extend([f"下方页图对应 PDF 文件第 {number} 页。", image_content(png)])
    except (RuntimeError, ValueError) as exc:
        raise ScoringError("PDF 指定页无法渲染。") from exc
    return parts


def citation_fragments(text: str):
    text = text.strip()
    while len(text) > 220:
        prefix = text[:220]
        boundary = max(prefix.rfind(" "), prefix.rfind("\n"), prefix.rfind("\t"))
        split = boundary if boundary >= 120 else 220
        yield text[:split].rstrip()
        text = text[split:].lstrip()
    if text:
        yield text


def annotated_material(units: list[str], source: str) -> tuple[str, dict[str, dict]]:
    output = []
    references = {}
    prefix = "P" if source == "pdf" else "L"
    for number, unit in enumerate(units, 1):
        if source == "pdf":
            output.append(f"<PDF page={number}>")
        for index, quote in enumerate(citation_fragments(unit), 1):
            reference = f"{prefix}{number}.{index}"
            output.append(f"{reference}: {quote}")
            references[reference] = {"source": source, "start": number, "end": number, "quote": quote}
        if source == "pdf":
            output.append("</PDF>")
    return "\n".join(output), references


def resolve_citations(result: dict, references: dict[str, dict]) -> None:
    for name in ("abstract", "theory", "empirical", "field"):
        section_value = result.get(name)
        if not isinstance(section_value, dict) or not isinstance(section_value.get("evidence"), list):
            continue
        for index, item in enumerate(section_value["evidence"]):
            if isinstance(item, dict) and "ref" in item:
                reference = item["ref"]
                ensure(isinstance(reference, str) and reference in references, f"{name} 的引文编号不在本次材料中。")
                section_value["evidence"][index] = {**references[reference], "ref": reference}


def evaluate_mode(gateway: Gateway, content: Content, mode: str, md_lines: list[str], pdf_pages: list[str],
                  image_pages: list[int], references: dict[str, dict]) -> dict:
    correction = ""
    for _ in range(2):
        instructions = RUBRIC
        if correction:
            instructions += f"\n重新按规则检查完整 JSON。上一次校验发现：{correction}"
        try:
            result = gateway.complete(content, instructions=instructions)
            status = result.get("status")
            if status == "ok":
                abstract = result.get("abstract")
                if mode == "md" and isinstance(abstract, dict) and abstract.get("found") is False:
                    return {"status": "needs_pdf", "reason": "MD 未识别摘要", "pages": []}
                resolve_citations(result, references)
                classify(result, mode)
                check_evidence(result, mode, md_lines, pdf_pages, image_pages)
            else:
                ensure(status in ("needs_pdf", "blocked") and valid_text(result.get("reason")), "未完成状态缺少合法类型和原因。")
                if status == "needs_pdf":
                    pages = result.get("pages")
                    ensure(isinstance(pages, list) and len(pages) <= 4 and all(type(p) is int and p > 0 for p in pages), "PDF 核查页码不合法。")
                    ensure(mode != "pdf" or bool(pages) and max(pages) <= len(pdf_pages), "PDF 文本阶段需给出有效的待核查页图。")
            return result
        except InvalidResult as exc:
            correction = str(exc)
    raise InvalidResult(correction)


def score(paper: Paper, gateway: Gateway) -> dict:
    with open_paper(paper) as (md, pdf):
        md_lines = md.splitlines()
        modes, pdf_pages, image_pages = [], [], []
        prefix = f"当前论文编号 {paper.id}，题目 {paper.title}。PDF 文件共 {pdf.page_count} 页。\n"
        # A missing MD abstract marker already requires checking the PDF.
        # Start there instead of requesting an MD verdict that cannot be saved.
        phase = "md" if ABSTRACT_MARKER.search("\n".join(md_lines[:450])) else "pdf"
        while True:
            modes.append(phase)
            if phase == "md":
                md_text, references = annotated_material(md_lines, "md")
                content = prefix + "当前材料为 MD，引用编号 L行号.段号。需要核查原文时请求 PDF。\n<MD>\n" + md_text + "\n</MD>"
            else:
                if not pdf_pages:
                    try:
                        pdf_pages = [page.get_text(sort=False) for page in pdf]
                    except (RuntimeError, ValueError) as exc:
                        raise ScoringError("无法提取 PDF 原文。") from exc
                    pdf_text, references = annotated_material(pdf_pages, "pdf")
                text = prefix + "以下是 PDF 原文，引用编号 P文件页码.段号。所提供 PDF 的附录均在范围内。\n" + pdf_text
                content = text if phase == "pdf" else [text, *page_images(pdf, image_pages)]
            try:
                result = evaluate_mode(gateway, content, phase, md_lines, pdf_pages, image_pages, references)
            except InvalidResult:
                if phase == "md":
                    phase = "pdf"
                    continue
                raise
            if result["status"] == "ok":
                label_one, label_two, path = classify(result, phase)
                return {"pdf_pages": pdf.page_count, "modes": modes, "image_pages": image_pages,
                        "label_one": label_one, "label_two": label_two, "field_path": path, "judgment": result}
            if phase == "md":
                phase = "pdf"
                continue
            if phase == "pdf" and result["status"] == "needs_pdf":
                if not gateway.settings.pdf_images:
                    raise ScoringError("关键证据需要回查 PDF 页图，当前未启用页图输入。")
                image_pages = sorted(set(result["pages"]))
                phase = "pdf_image"
                continue
            reason = re.sub(r"\s+", " ", result["reason"]).strip()[:250]
            raise ScoringError(f"模型无法完成必要的 PDF 核查：{reason}")

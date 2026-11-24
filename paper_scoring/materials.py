from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
import unicodedata
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
import pymupdf


class ScoringError(Exception):
    """A failure whose message is safe to put in local progress files."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    pdf_images: bool = True
    timeout: int = 180
    max_output_tokens: int = 8192
    pdf_font: str = ""

    @classmethod
    def read(cls, env_file: Path) -> Settings:
        load_dotenv(env_file, override=False, interpolate=False)
        prefix = "PAPER_SCORING_"
        base = os.environ.get(prefix + "BASE_URL", "").strip().rstrip("/")
        parts = urlsplit(base)
        local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        if not parts.hostname or (parts.scheme != "https" and not (local and parts.scheme == "http")):
            raise ScoringError("PAPER_SCORING_BASE_URL 应为 HTTPS API 地址。")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ScoringError("API 地址不能包含凭据、查询参数或片段。")
        base = urlunsplit(parts._replace(path=parts.path.rstrip("/") or "/v1"))
        key = os.environ.get(prefix + "API_KEY", "").strip()
        model = os.environ.get(prefix + "MODEL", "").strip()
        if not key or key == "replace-me" or not re.fullmatch(r"[\x21-\x7e]+", key):
            raise ScoringError("请在本地 .env 设置有效的 PAPER_SCORING_API_KEY。")
        if not model:
            raise ScoringError("请在本地 .env 设置 PAPER_SCORING_MODEL。")
        try:
            timeout = int(os.environ.get(prefix + "TIMEOUT", "180"))
            max_tokens = int(os.environ.get(prefix + "MAX_OUTPUT_TOKENS", "8192"))
        except ValueError as exc:
            raise ScoringError("超时和输出 token 上限必须为整数。") from exc
        images = os.environ.get(prefix + "PDF_IMAGES", "true").strip().lower()
        if timeout < 1 or max_tokens < 256 or images not in {"true", "false"}:
            raise ScoringError("请检查 TIMEOUT、MAX_OUTPUT_TOKENS 和 PDF_IMAGES 设置。")
        return cls(base, model, key, images == "true", timeout, max_tokens, os.environ.get(prefix + "PDF_FONT", ""))

    def model_config(self) -> dict:
        return {"api_base_url": self.base_url, "model": self.model, "pdf_images": self.pdf_images,
                "max_output_tokens": self.max_output_tokens, "temperature": 0, "thinking": "disabled",
                "reasoning_effort": "none",
                "agent_framework": f"Pydantic AI {version('pydantic-ai-slim')}"}


@dataclass(frozen=True)
class Paper:
    id: str
    position: int
    title: str
    md: Path | None
    pdf: Path | None
    md_sha256: str | None = None
    pdf_sha256: str | None = None
    issue: str = ""

    @property
    def group(self) -> str:
        return f"组{(self.position - 1) // 20 + 1:02d}"

    def snapshot(self) -> dict:
        return {"id": self.id, "position": self.position, "title": self.title, "group": self.group,
                "md_sha256": self.md_sha256, "pdf_sha256": self.pdf_sha256, "ready": not self.issue}


@dataclass(frozen=True)
class Manifest:
    name: str
    papers: tuple[Paper, ...]
    instructions: Path | None
    instructions_sha256: str | None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def indexed_files(directory: Path, extension: str, wanted: set[str]) -> dict[str, list[Path]]:
    if not directory.is_dir():
        raise ScoringError(f"材料目录不存在：{directory}")
    matches = {paper_id: [] for paper_id in wanted}
    for path in directory.iterdir():
        number = path.stem.partition("-")[0]
        if number in wanted and path.suffix.lower() == extension and path.is_file():
            matches[number].append(path)
    return matches


def load_manifest(path: Path) -> Manifest:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        name = data["name"]
        ids = data["ids"]
        pdf_dir = path.parent / data["pdf_dir"]
        md_dir = path.parent / data["md_dir"]
        instructions = path.parent / data["instructions"] if "instructions" in data else None
        titles = data.get("titles", {})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ScoringError(f"无法读取论文清单：{path}") from exc
    if not isinstance(name, str) or not name.strip():
        raise ScoringError("清单 name 必须为非空字符串。")
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not re.fullmatch(r"[0-9]{3,}", i) for i in ids):
        raise ScoringError("ids 必须为带前导零的编号字符串列表。")
    if len(set(ids)) != len(ids) or len({int(i) for i in ids}) != len(ids):
        raise ScoringError("清单中有重复的论文编号。")
    if not isinstance(titles, dict) or any(number not in ids or not isinstance(title, str) or not title.strip()
                                           for number, title in titles.items()):
        raise ScoringError("清单 titles 只接受已列论文编号和非空标题。")
    ordered = sorted(ids, key=int)
    pdfs = indexed_files(pdf_dir, ".pdf", set(ids))
    mds = indexed_files(md_dir, ".md", set(ids))
    papers = []
    for position, number in enumerate(ordered, 1):
        pdf_paths, md_paths = pdfs[number], mds[number]
        pdf = pdf_paths[0].resolve() if len(pdf_paths) == 1 else None
        md = md_paths[0].resolve() if len(md_paths) == 1 else None
        named = pdf or md
        file_title = normalize(named.stem).partition("-")[2].strip() if named else ""
        title = titles.get(number, file_title or f"论文 {number}").strip()
        if not pdf or not md:
            issue = f"PDF {len(pdf_paths)} 份，MD {len(md_paths)} 份，要求各一份。"
        elif normalize(pdf.stem) != normalize(md.stem) or not file_title:
            issue = "PDF 和 MD 的文件名不一致或缺少标题。"
        else:
            try:
                md_bytes, pdf_bytes = md.read_bytes(), pdf.read_bytes()
                if not md_bytes.decode("utf-8-sig").strip() or not pdf_bytes:
                    raise ValueError("empty paper")
                with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
                    if not doc.is_pdf or doc.needs_pass or not doc.page_count:
                        raise ValueError("unreadable PDF")
            except (OSError, ValueError, RuntimeError):
                issue = "PDF 或 UTF-8 MD 无法读取、为空或加密。"
            else:
                papers.append(Paper(number, position, title, md, pdf, sha256(md_bytes), sha256(pdf_bytes)))
                continue
        papers.append(Paper(number, position, title, md, pdf, issue=issue))
    try:
        instructions_hash = sha256(instructions.read_bytes()) if instructions else None
    except OSError as exc:
        raise ScoringError(f"无法读取正式指令：{instructions}") from exc
    return Manifest(name, tuple(papers), instructions.resolve() if instructions else None, instructions_hash)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def write_json(path: Path, data: dict) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

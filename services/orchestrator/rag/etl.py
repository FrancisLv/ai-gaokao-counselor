"""招生章程 ETL：解析 JSON/TXT → 按条切分 → 智能合并 → 输出 chunk 列表。

切分策略（详见 docs/dev-log.md 第 1 条）：
- 以「第X条」为语义最小单元
- 短条合并：连续短条累计 < 300 字时合并，直到落入 300-600 字区间
- 长条拆分：超过 600 字的条按段落/语义边界拆分
- 每个 chunk 携带父级路径作为 section_path
- 重叠策略：每个 chunk 带上所在章的概述句作为前缀
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


# ---------- 数据结构 ----------

@dataclass
class DocMeta:
    """文档级元数据，从 JSON 文件解析。"""
    title: str = ""
    school_name: str = ""
    school_id: str = ""
    year: int = 2025
    url: str = ""
    update_time: str = ""


@dataclass
class Chunk:
    """一个检索单元。"""
    text: str
    section_path: str  # 如「第三章 计划与录取 > 第十条」
    meta: dict = field(default_factory=dict)  # 冗余 school_name / year 等


# ---------- 常量 ----------

# 匹配「第X章  标题」或「第X章 标题」，支持前导全角/半角空格
_RE_CHAPTER = re.compile(r"^[\s  ]*(第[一二三四五六七八九十百千\d]+章)\s*(.*)")
# 匹配「第X条  内容」或「第X条 内容」，支持前导全角/半角空格
_RE_ARTICLE = re.compile(r"^[\s  ]*(第[一二三四五六七八九十百千\d]+条)\s*(.*)")

# 中文数字格式：「一、」「二、」「三、」...（上海市招生章程常用）
# 只匹配行首无缩进的中文数字（顶层章节）
_RE_CN_NUM_SECTION = re.compile(r"^([一二三四五六七八九十百]+)[、.．]\s*(.*)")

# 阿拉伯数字格式：「1.」「2.」「3.」...
_RE_ARABIC_SECTION = re.compile(r"^[\s  ]*(\d+)[.．、](?!\d)\s*(.*)")

MIN_CHUNK_LEN = 300
MAX_CHUNK_LEN = 600


# ---------- 解析 ----------

def parse_json_meta(json_path: Path) -> DocMeta:
    """从 JSON 文件提取元数据。"""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return DocMeta(
        title=data.get("title", ""),
        school_name=data.get("school_name", ""),
        school_id=str(data.get("school_id", "")),
        year=data.get("year", 2025),
        url=data.get("url", ""),
        update_time=data.get("update_time", ""),
    )


def parse_txt_content(txt_path: Path) -> str:
    """从 TXT 文件提取纯文本，跳过头部元信息。"""
    lines = txt_path.read_text(encoding="utf-8").splitlines()
    # 跳过头部（标题/学校/来源/更新时间 + 分隔线）
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("===="):
            start = i + 1
            break
    return "\n".join(lines[start:]).strip()


# ---------- 切分 ----------

def _split_into_sections(text: str) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """将全文按章/条拆分为结构化列表。

    返回: [(chapter_title, chapter_summary, [(article_title, article_text), ...])]
    """
    chapters: list[tuple[str, str, list[tuple[str, str]]]] = []
    current_chapter = ""
    current_chapter_lines: list[str] = []
    articles: list[tuple[str, str]] = []
    current_article_title = ""
    current_article_lines: list[str] = []

    def _flush_article():
        nonlocal current_article_title, current_article_lines
        if current_article_title or current_article_lines:
            article_text = "\n".join(current_article_lines).strip()
            if article_text:
                articles.append((current_article_title, article_text))
            current_article_title = ""
            current_article_lines = []

    def _flush_chapter():
        nonlocal current_chapter, current_chapter_lines, articles
        _flush_article()
        if current_chapter or articles or current_chapter_lines:
            # 提取章概述：取章标题后第一行非空文本（通常是对本章的概括）
            summary = ""
            for line in current_chapter_lines:
                stripped = line.strip()
                if stripped and not _RE_ARTICLE.match(stripped):
                    summary = stripped
                    break
            # 如果没有条（articles 为空），把所有行作为内容（适用于中文数字格式）
            if not articles and current_chapter_lines:
                # 将所有行合并为内容
                full_content = "\n".join(current_chapter_lines).strip()
                chapters.append((current_chapter, full_content, []))
            else:
                # 章概述仅用于上下文重复；完整前言仍须作为正文保留。
                if current_chapter_lines:
                    articles.insert(0, ("章前言", "\n".join(current_chapter_lines).strip()))
                chapters.append((current_chapter, summary, list(articles)))
        current_chapter = ""
        current_chapter_lines = []
        articles = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # 检查是否是新章（标准格式：第X章）
        m_chapter = _RE_CHAPTER.match(stripped)
        if m_chapter:
            _flush_chapter()
            chapter_name = m_chapter.group(1)
            chapter_subtitle = m_chapter.group(2).strip()
            current_chapter = f"{chapter_name} {chapter_subtitle}".strip() if chapter_subtitle else chapter_name
            continue

        # 检查是否是新条（标准格式：第X条）
        m_article = _RE_ARTICLE.match(stripped)
        if m_article:
            _flush_article()
            current_article_title = m_article.group(1)
            rest = m_article.group(2).strip()
            if rest:
                current_article_lines.append(rest)
            continue

        # 检查中文数字格式（一、二、三...）作为章
        m_cn = _RE_CN_NUM_SECTION.match(stripped)
        if m_cn and not current_article_title:
            # 只有在没有正在解析的条时，才将中文数字视为章
            _flush_chapter()
            cn_num = m_cn.group(1)
            cn_title = m_cn.group(2).strip()
            current_chapter = f"{cn_num}、{cn_title}".strip() if cn_title else f"{cn_num}、"
            continue

        m_arabic = _RE_ARABIC_SECTION.match(stripped)
        if m_arabic and not current_article_title:
            _flush_chapter()
            current_chapter = f"{m_arabic.group(1)}. {m_arabic.group(2).strip()}".strip()
            continue

        # 普通行
        if not articles and not current_article_title:
            # 章标题后、第一条前的内容 → 章概述
            current_chapter_lines.append(stripped)
        else:
            current_article_lines.append(stripped)

    _flush_chapter()
    return chapters


def _split_long_text(text: str, max_len: int = MAX_CHUNK_LEN) -> list[str]:
    """将超长文本按段落边界拆分，每段不超过 max_len 字。"""
    if max_len <= 0:
        raise ValueError("max_len 必须为正整数")
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_len, len(text))
        if end < len(text):
            window = text[start:end]
            boundary = window.rfind("\n")
            if boundary < 0:
                boundary = max(window.rfind(mark) for mark in "。；;！？!?")
            # 没有段落/句子边界时硬切，避免单个超长句突破上限。
            if boundary >= 0:
                end = start + boundary + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def _merge_short_articles(
    articles: list[tuple[str, str]],
    chapter_prefix: str,
    max_len: int = MAX_CHUNK_LEN,
) -> list[Chunk]:
    """将短条合并到 300-600 字区间，输出 Chunk 列表。"""
    if max_len <= 0:
        raise ValueError("max_len 必须为正整数")
    chunks: list[Chunk] = []
    buf_text = ""
    buf_articles: list[str] = []

    def _flush():
        nonlocal buf_text, buf_articles
        if not buf_text:
            return
        # 构造 section_path
        if len(buf_articles) == 1:
            path = f"{chapter_prefix} > {buf_articles[0]}"
        else:
            path = f"{chapter_prefix} > {buf_articles[0]}~{buf_articles[-1]}"
        chunks.append(Chunk(text=buf_text.strip(), section_path=path))
        buf_text = ""
        buf_articles = []

    for title, text in articles:
        # 如果单条已超 MAX，先 flush 缓存，再单独拆分
        if len(text) > max_len:
            _flush()
            sub_chunks = _split_long_text(text, max_len)
            for sc in sub_chunks:
                chunks.append(Chunk(text=sc, section_path=f"{chapter_prefix} > {title}"))
            continue

        # 尝试合并
        candidate = f"{buf_text}\n{text}".strip() if buf_text else text
        if len(candidate) > max_len:
            _flush()
            candidate = text
        if len(candidate) >= min(MIN_CHUNK_LEN, max_len):
            # 合并后达到下限 → 产出
            buf_text = candidate
            buf_articles.append(title)
            _flush()
        else:
            buf_text = candidate
            buf_articles.append(title)

    # 处理尾部短条：如果 < MIN_CHUNK_LEN，合并到上一个 chunk
    if buf_text and len(buf_text) < MIN_CHUNK_LEN:
        if chunks and len(chunks[-1].text) + 1 + len(buf_text) <= max_len:
            # 合并到最后一个 chunk
            last_chunk = chunks[-1]
            last_chunk.text = f"{last_chunk.text}\n{buf_text}".strip()
            # 更新 section_path 以包含新合并的条
            last_chunk.section_path = f"{last_chunk.section_path.split('~')[0]}~{buf_articles[-1]}"
        else:
            # 没有前一个 chunk，直接输出（即使短）
            _flush()
    elif buf_text:
        _flush()

    return chunks


def split_charter(text: str, meta: DocMeta) -> list[Chunk]:
    """将一份章程文本切分为 Chunk 列表。

    核心入口。实现「条为原子 + 智能合并」策略。
    """
    sections = _split_into_sections(text)
    all_chunks: list[Chunk] = []

    # 对于没有「条」的格式（如中文数字格式），合并连续短章
    buf_text = ""
    buf_sections: list[str] = []

    def _flush_buf():
        nonlocal buf_text, buf_sections
        if not buf_text:
            return
        # 构造 section_path
        if len(buf_sections) == 1:
            path = buf_sections[0]
        else:
            path = f"{buf_sections[0]}~{buf_sections[-1]}"
        # 如果超过 MAX_CHUNK_LEN，拆分
        if len(buf_text) > MAX_CHUNK_LEN:
            sub_parts = _split_long_text(buf_text, MAX_CHUNK_LEN)
            for part in sub_parts:
                all_chunks.append(Chunk(text=part, section_path=path))
        else:
            all_chunks.append(Chunk(text=buf_text, section_path=path))
        buf_text = ""
        buf_sections = []

    for chapter_title, chapter_summary, articles in sections:
        chapter_prefix = chapter_title or "前言"

        # 章概述前缀
        prefix = f"【{chapter_prefix}】{chapter_summary}" if chapter_summary else f"【{chapter_prefix}】"

        if not articles:
            # 没有条，整章作为内容
            if chapter_summary or chapter_title:
                # 尝试合并短章
                candidate = f"{buf_text}\n{prefix}".strip() if buf_text else prefix
                if len(candidate) > MAX_CHUNK_LEN and buf_text:
                    _flush_buf()
                    candidate = prefix
                if len(candidate) >= MIN_CHUNK_LEN:
                    # 合并后达到下限 → 产出
                    buf_text = candidate
                    buf_sections.append(chapter_prefix)
                    _flush_buf()
                else:
                    buf_text = candidate
                    buf_sections.append(chapter_prefix)
            continue

        # 有条的情况，先 flush 之前的缓冲
        _flush_buf()

        # 重复上下文限制长度，完整章名在路径中、完整概述在正文中。
        summary = re.split(r"(?<=[。；;！？!?])", chapter_summary, maxsplit=1)[0][:80]
        prefix = f"【{chapter_prefix[:100]}】{summary}"
        merged = _merge_short_articles(articles, chapter_prefix, MAX_CHUNK_LEN - len(prefix) - 1)

        # 给每个 chunk 加上章概述前缀
        for chunk in merged:
            chunk.text = f"{prefix}\n{chunk.text}"

        all_chunks.extend(merged)

    # 处理尾部缓冲
    _flush_buf()

    for chunk in all_chunks:
        chunk.meta = {
            "school_name": meta.school_name,
            "year": meta.year,
            "url": meta.url,
        }
    return all_chunks


# ---------- 文件遍历 ----------

def iter_charter_files(charter_dir: str | Path) -> Iterator[tuple[Path, Path]]:
    """遍历章程目录，产出 (json_path, txt_path) 对。

    跳过以 _ 开头的辅助文件。
    """
    base = Path(charter_dir)
    json_files = sorted(base.glob("*.json"))
    for jf in json_files:
        if jf.name.startswith("_"):
            continue
        tf = jf.with_suffix(".txt")
        if tf.exists():
            yield jf, tf


def process_single_file(json_path: Path, txt_path: Path) -> tuple[DocMeta, list[Chunk]]:
    """处理一对 JSON+TXT 文件，返回元数据和 chunk 列表。"""
    meta = parse_json_meta(json_path)
    text = parse_txt_content(txt_path)
    chunks = split_charter(text, meta)

    return meta, chunks

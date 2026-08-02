"""保留原文坐标的 Markdown 章节与证据解析器。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import List, Sequence

from .contracts import KnowledgeEvidence, KnowledgeSection, KnowledgeValidationError


_HEADING_RE = re.compile(r"(?m)^(#{1,6})[ \t]+([^\r\n]+)")


@dataclass(frozen=True)
class ParsedKnowledgeDocument:
    """一次确定性解析产生的章节和证据。"""

    title: str
    parser_version: str
    sections: Sequence[KnowledgeSection]
    evidence: Sequence[KnowledgeEvidence]


def parse_markdown_source(
    content: str,
    requested_title: str,
    tenant_id: str,
    document_id: str,
    document_version: int,
    max_input_bytes: int = 10 * 1024 * 1024,
    max_sections: int = 2048,
    evidence_max_chars: int = 1800,
    evidence_overlap_chars: int = 200,
) -> ParsedKnowledgeDocument:
    """解析 Markdown，但不改写原文，所有引用坐标均指向原始 UTF-8 字节。"""

    if not isinstance(content, str):
        raise KnowledgeValidationError("content 必须是字符串")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as error:
        raise KnowledgeValidationError("content 不是有效的 UTF-8 文本") from error
    if len(encoded) > max_input_bytes:
        raise KnowledgeValidationError(
            "content 超过大小限制: %s > %s" % (len(encoded), max_input_bytes)
        )
    if "\x00" in content:
        raise KnowledgeValidationError("content 不能包含 NUL 字符")
    if max_sections <= 0 or evidence_max_chars <= 0:
        raise KnowledgeValidationError("解析资源限制必须大于零")
    if evidence_overlap_chars < 0 or evidence_overlap_chars >= evidence_max_chars:
        raise KnowledgeValidationError("证据重叠长度必须小于证据长度")

    matches = list(_HEADING_RE.finditer(content))
    section_ranges = _section_ranges(content, matches)
    if len(section_ranges) > max_sections:
        raise KnowledgeValidationError(
            "章节数量超过限制: %s > %s" % (len(section_ranges), max_sections)
        )

    evidence_ranges_by_section = [
        _evidence_ranges(
            content,
            char_start,
            char_end,
            evidence_max_chars,
            evidence_overlap_chars,
        )
        for char_start, char_end, _ in section_ranges
    ]
    boundary_positions = {0, len(content)}
    for char_start, char_end, _ in section_ranges:
        boundary_positions.update((char_start, char_end))
    for ranges in evidence_ranges_by_section:
        for quote_start, quote_end in ranges:
            boundary_positions.update((quote_start, quote_end))
    byte_offsets = _selected_byte_offsets(content, encoded, boundary_positions)

    sections: List[KnowledgeSection] = []
    evidence: List[KnowledgeEvidence] = []
    evidence_index = 0
    for section_index, (char_start, char_end, heading) in enumerate(section_ranges):
        section_text = content[char_start:char_end]
        section_id = _stable_id(
            "%s:%s:section:%s" % (document_id, document_version, section_index)
        )
        sections.append(
            KnowledgeSection(
                section_id=section_id,
                tenant_id=tenant_id,
                document_id=document_id,
                document_version=document_version,
                section_index=section_index,
                heading=heading,
                char_start=char_start,
                char_end=char_end,
                byte_start=byte_offsets[char_start],
                byte_end=byte_offsets[char_end],
                content_hash=_sha256(section_text),
            )
        )
        for quote_start, quote_end in evidence_ranges_by_section[section_index]:
            quote = content[quote_start:quote_end]
            evidence_id = _stable_id(
                "%s:%s:evidence:%s" % (
                    document_id,
                    document_version,
                    evidence_index,
                )
            )
            evidence.append(
                KnowledgeEvidence(
                    evidence_id=evidence_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    document_version=document_version,
                    section_id=section_id,
                    evidence_index=evidence_index,
                    quote=quote,
                    char_start=quote_start,
                    char_end=quote_end,
                    byte_start=byte_offsets[quote_start],
                    byte_end=byte_offsets[quote_end],
                    quote_hash=_sha256(quote),
                )
            )
            evidence_index += 1

    parsed_title = requested_title.strip()
    for match in matches:
        if len(match.group(1)) == 1 and match.group(2).strip():
            parsed_title = match.group(2).strip()
            break
    return ParsedKnowledgeDocument(
        title=parsed_title,
        parser_version="source-preserving-markdown-v1",
        sections=sections,
        evidence=evidence,
    )


def _section_ranges(content: str, matches: Sequence[re.Match]) -> List[tuple]:
    """按标题边界生成互不重叠且覆盖全部非空原文的章节。"""

    if not content:
        return []
    ranges = []
    if not matches:
        return [(0, len(content), "")]
    if content[: matches[0].start()].strip():
        ranges.append((0, matches[0].start(), ""))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        ranges.append((match.start(), end, match.group(2).strip()))
    return ranges


def _evidence_ranges(
    content: str,
    section_start: int,
    section_end: int,
    max_chars: int,
    overlap_chars: int,
) -> Sequence[tuple]:
    """在章节内生成可重放的固定上限证据窗口。"""

    ranges = []
    cursor = section_start
    while cursor < section_end:
        end = min(section_end, cursor + max_chars)
        if end < section_end:
            boundary = _last_boundary(content, cursor, end)
            if boundary > cursor + max_chars // 2:
                end = boundary
        quote_start, quote_end = _trim_range(content, cursor, end)
        if quote_start < quote_end:
            ranges.append((quote_start, quote_end))
        if end >= section_end:
            break
        cursor = max(cursor + 1, end - overlap_chars)
    return ranges


def _last_boundary(content: str, start: int, end: int) -> int:
    """优先在段落或句末切分，同时保持原文坐标。"""

    candidates = []
    for marker in ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? "):
        index = content.rfind(marker, start, end)
        if index >= start:
            candidates.append(index + len(marker))
    return max(candidates) if candidates else end


def _trim_range(content: str, start: int, end: int) -> tuple:
    """去除窗口首尾空白并同步调整坐标。"""

    while start < end and content[start].isspace():
        start += 1
    while end > start and content[end - 1].isspace():
        end -= 1
    return start, end


def _selected_byte_offsets(
    content: str, encoded: bytes, positions: Sequence[int]
) -> dict:
    """只为实际引用边界计算 UTF-8 偏移，避免逐字符重复编码。"""

    offsets = {0: 0, len(content): len(encoded)}
    previous_position = 0
    previous_offset = 0
    for position in sorted(set(positions)):
        if position in offsets:
            previous_position = position
            previous_offset = offsets[position]
            continue
        previous_offset += len(content[previous_position:position].encode("utf-8"))
        offsets[position] = previous_offset
        previous_position = position
    return offsets


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

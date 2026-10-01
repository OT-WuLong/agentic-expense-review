"""Pure preflight, canonicalization, chunking, and fixture field extraction."""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pypdf import PdfReader

from app.ingestion.models import (
    BBox,
    DocumentChunk,
    LocatedField,
    PageContent,
    ParsedBlock,
    ParseStatus,
    PdfPreflight,
)
from app.models import ExtractionStatus

MAX_PDF_BYTES = 200 * 1024 * 1024
MAX_PDF_PAGES = 200
CANONICALIZER_VERSION = "2"
BLOCK_SEPARATOR = "\n\n"
_AUXILIARY_KINDS = {
    "header",
    "footer",
    "page_number",
    "aside_text",
    "page_footnote",
}
_TABLE_KINDS = {"table", "table_body"}
_CHAPTER_HEADING = re.compile(r"^第[一二三四五六七八九十百零〇0-9]+章(?:\s|$)")
_NUMBERED_SUBHEADING = re.compile(r"^\d+(?:\.\d+)+\s+\S")


# 页内块跨度：块本体、规范化文本、标题路径及其在页文本中的字符区间
@dataclass(frozen=True, slots=True)
class _BlockSpan:
    block: ParsedBlock
    text: str
    title_path: tuple[str, ...]
    start: int
    end: int


# 分块计算文件 SHA-256，避免将整个文件读入内存
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# 本地预检 PDF：拒绝空/超大/加密/超页数/损坏文件，并判断是否需要 OCR
def preflight_pdf(
    path: str | Path,
    *,
    max_bytes: int = MAX_PDF_BYTES,
    max_pages: int = MAX_PDF_PAGES,
) -> PdfPreflight:
    """Reject unsupported PDFs locally and choose the initial MinerU OCR route."""

    pdf_path = Path(path)
    display_path = str(pdf_path)
    try:
        size_bytes = pdf_path.stat().st_size
    except OSError as exc:
        return PdfPreflight(
            path=display_path,
            status=ParseStatus.CORRUPT,
            size_bytes=0,
            message=f"cannot read PDF: {exc}",
        )

    if size_bytes == 0:
        return PdfPreflight(
            path=display_path,
            status=ParseStatus.EMPTY,
            file_sha256=hashlib.sha256(b"").hexdigest(),
            size_bytes=0,
            message="PDF is empty",
        )
    if size_bytes > max_bytes:
        return PdfPreflight(
            path=display_path,
            status=ParseStatus.TOO_LARGE,
            size_bytes=size_bytes,
            message=f"PDF exceeds {max_bytes} bytes",
        )

    file_sha256 = _sha256_file(pdf_path)
    try:
        reader = PdfReader(pdf_path, strict=False)
        if reader.is_encrypted:
            return PdfPreflight(
                path=display_path,
                status=ParseStatus.ENCRYPTED,
                file_sha256=file_sha256,
                size_bytes=size_bytes,
                message="encrypted PDF is not accepted",
            )
        page_count = len(reader.pages)
        if page_count == 0:
            return PdfPreflight(
                path=display_path,
                status=ParseStatus.EMPTY,
                file_sha256=file_sha256,
                size_bytes=size_bytes,
                page_count=0,
                message="PDF contains no pages",
            )
        if page_count > max_pages:
            return PdfPreflight(
                path=display_path,
                status=ParseStatus.TOO_MANY_PAGES,
                file_sha256=file_sha256,
                size_bytes=size_bytes,
                page_count=page_count,
                message=f"PDF exceeds {max_pages} pages",
            )

        native_text_chars = 0
        suspicious_text_chars = 0
        for page in reader.pages:
            try:
                extracted = re.sub(r"\s+", "", page.extract_text() or "")
                native_text_chars += len(extracted)
                suspicious_text_chars += sum(
                    character == "\ufffd"
                    or unicodedata.category(character) in {"Cc", "Co", "Cs"}
                    for character in extracted
                )
            except Exception:  # noqa: BLE001,S112 - advisory probe; MinerU is authoritative.
                continue
    except Exception as exc:  # noqa: BLE001 - malformed PDFs can raise multiple libraries' errors.
        return PdfPreflight(
            path=display_path,
            status=ParseStatus.CORRUPT,
            file_sha256=file_sha256,
            size_bytes=size_bytes,
            message=f"cannot parse PDF: {exc}",
        )

    garbled = bool(native_text_chars) and suspicious_text_chars / native_text_chars > 0.05
    needs_ocr = native_text_chars == 0 or garbled
    return PdfPreflight(
        path=display_path,
        status=ParseStatus.NEEDS_OCR if needs_ocr else ParseStatus.READY,
        file_sha256=file_sha256,
        size_bytes=size_bytes,
        page_count=page_count,
        native_text_chars=native_text_chars,
        suspicious_text_chars=suspicious_text_chars,
        suggested_ocr=needs_ocr,
        message=("native text layer appears garbled" if garbled else "no native text layer")
        if needs_ocr
        else None,
    )


# 文本规范化：NFC、统一换行、折叠空白与多余空行
def _normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[^\S\n]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


# 表格规范化：把 HTML 表格还原为管道分隔文本，再走通用规范化
def _normalize_table(value: str) -> str:
    if "<" not in value or ">" not in value:
        return _normalize_text(value)
    value = re.sub(r"<\s*br\s*/?\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<\s*/\s*(?:td|th)\s*>", " | ", value, flags=re.IGNORECASE)
    value = re.sub(r"<\s*/\s*tr\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"(?:\s*\|\s*)+\n", "\n", html.unescape(value))
    return _normalize_text(value)


# 按块类型选择表格或普通文本的规范化方式
def _normalized_block_text(block: ParsedBlock) -> str:
    if block.kind.casefold() in _TABLE_KINDS:
        return _normalize_table(block.text)
    return _normalize_text(block.text)


# 维护标题栈：按层级更新并截断更深层级，用于生成 title_path
def _update_title_stack(stack: list[str], level: int, title: str) -> None:
    while len(stack) < level:
        stack.append("")
    stack[level - 1] = title
    del stack[level:]


# 解析清单里的字符串列表元数据，元素必须为非空字符串
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError("manifest list metadata must contain non-empty strings")
    return list(value)


# 读取清单文档的必填非空字符串字段
def _required_string(document: Mapping[str, object], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"manifest document requires {key}")
    return value


# 读取清单文档的可选字符串字段：允许 null，但不允许空串
def _optional_string(document: Mapping[str, object], key: str) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"manifest {key} must be a non-empty string or null")
    return value


# 把来源清单条目转换为 chunk 元数据，并校验 priority/synthetic/来源哈希
def _chunk_metadata(document: Mapping[str, object]) -> dict[str, object]:
    is_policy = "expense_types" in document or "department_ids" in document
    priority = document.get("priority")
    if priority is not None and (not isinstance(priority, int) or isinstance(priority, bool)):
        raise ValueError("manifest priority must be an integer or null")
    synthetic = document.get("synthetic", False)
    if not isinstance(synthetic, bool):
        raise TypeError("manifest synthetic must be boolean")
    source_sha256 = document.get("source_sha256") or document.get("pdf_sha256")
    if not isinstance(source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("manifest document requires a lowercase source SHA-256")
    return {
        "document_id": _required_string(document, "document_id"),
        "source_kind": "POLICY" if is_policy else "ATTACHMENT",
        "title": _optional_string(document, "title"),
        "version": _required_string(document, "version"),
        "catalog_snapshot_id": _optional_string(document, "catalog_snapshot_id"),
        "published_status": _optional_string(document, "published_status"),
        "authority_level": _optional_string(document, "authority_level"),
        "priority": priority,
        "supersedes_document_id": _optional_string(document, "supersedes_document_id"),
        "department_ids": _string_list(document.get("department_ids")),
        "expense_types": _string_list(document.get("expense_types")),
        "effective_from": document.get("effective_from"),
        "effective_to": document.get("effective_to"),
        "fixture_key": _optional_string(document, "fixture_key"),
        "document_type": _optional_string(document, "document_type"),
        "media_type": _optional_string(document, "media_type"),
        "source_type": _optional_string(document, "source_type"),
        "reference_source_ids": _string_list(document.get("reference_source_ids")),
        "license_id": _optional_string(document, "license_id"),
        "synthetic": synthetic,
        "source_sha256": source_sha256,
        "pdf_sha256": _optional_string(document, "pdf_sha256"),
    }


# 由块跨度构造 chunk：用来源哈希+页码+字符范围+正文哈希生成确定性 chunk_id
def _make_chunk(
    page: PageContent,
    spans: Sequence[_BlockSpan],
    *,
    metadata: Mapping[str, object],
    parse_artifact_sha256: str,
    canonicalizer_version: str,
) -> DocumentChunk:
    start = spans[0].start
    end = spans[-1].end
    chunk_text = page.text[start:end]
    identity = json.dumps(
        [
            metadata["source_sha256"],
            canonicalizer_version,
            metadata["document_id"],
            metadata["version"],
            page.page_idx,
            start,
            end,
            hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return DocumentChunk(
        chunk_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        page_idx=page.page_idx,
        page_number=page.page_number,
        title_path=list(spans[-1].title_path),
        char_start=start,
        char_end=end,
        text=chunk_text,
        block_indices=[span.block.block_index for span in spans],
        bboxes=[span.block.bbox for span in spans if span.block.bbox is not None],
        parse_artifact_sha256=parse_artifact_sha256,
        canonicalizer_version=canonicalizer_version,
        **metadata,
    )


# 规范化并分块：按页拼接文本，表格独立成块，标题变化或超长时切分，且不跨页
def canonicalize_and_chunk(
    blocks: Iterable[ParsedBlock],
    document: Mapping[str, object],
    *,
    parse_artifact_sha256: str,
    canonicalizer_version: str = CANONICALIZER_VERSION,
    max_chunk_chars: int = 1200,
) -> tuple[list[PageContent], list[DocumentChunk]]:
    """Create deterministic page-relative chunks without crossing page boundaries."""

    if max_chunk_chars < 1:
        raise ValueError("max_chunk_chars must be positive")
    if not re.fullmatch(r"[0-9a-f]{64}", parse_artifact_sha256):
        raise ValueError("parse_artifact_sha256 must be a lowercase SHA-256 digest")
    if not canonicalizer_version:
        raise ValueError("canonicalizer_version cannot be empty")

    ordered = sorted(blocks, key=lambda block: (block.page_idx, block.block_index))
    block_indices = [block.block_index for block in ordered]
    if len(block_indices) != len(set(block_indices)):
        raise ValueError("ParsedBlock.block_index must be unique within a document")

    title_stack: list[str] = []
    page_entries: dict[int, list[tuple[ParsedBlock, str, tuple[str, ...]]]] = {}
    for block in ordered:
        kind = block.kind.casefold()
        if kind in _AUXILIARY_KINDS:
            continue
        text = _normalized_block_text(block)
        if not text:
            continue
        level = block.text_level if block.text_level and block.text_level > 0 else None
        if level is None and kind == "title":
            level = 1
        if level is not None and _CHAPTER_HEADING.match(text):
            level = 1
        elif level is not None and _NUMBERED_SUBHEADING.match(text):
            level = 2
        if level is not None:
            _update_title_stack(title_stack, level, text)
        title_path = tuple(title for title in title_stack if title)
        page_entries.setdefault(block.page_idx, []).append((block, text, title_path))

    metadata = _chunk_metadata(document)
    pages: list[PageContent] = []
    chunks: list[DocumentChunk] = []
    for page_idx, entries in sorted(page_entries.items()):
        page_text = BLOCK_SEPARATOR.join(text for _, text, _ in entries)
        page = PageContent(page_idx=page_idx, page_number=page_idx + 1, text=page_text)
        pages.append(page)

        spans: list[_BlockSpan] = []
        cursor = 0
        for index, (block, text, title_path) in enumerate(entries):
            if index:
                cursor += len(BLOCK_SEPARATOR)
            start = cursor
            cursor += len(text)
            spans.append(
                _BlockSpan(
                    block=block,
                    text=text,
                    title_path=title_path,
                    start=start,
                    end=cursor,
                )
            )

        pending: list[_BlockSpan] = []
        for span in spans:
            table = span.block.kind.casefold() in _TABLE_KINDS
            would_overflow = bool(pending) and span.end - pending[0].start > max_chunk_chars
            title_changed = bool(pending) and span.title_path != pending[-1].title_path
            descends = bool(pending) and (
                span.title_path[: len(pending[-1].title_path)]
                == pending[-1].title_path
            )
            pending_is_headings = bool(pending) and all(
                item.block.text_level is not None or item.block.kind.casefold() == "title"
                for item in pending
            )
            if pending and (
                table
                or would_overflow
                or (title_changed and not (pending_is_headings and descends))
            ):
                chunks.append(
                    _make_chunk(
                        page,
                        pending,
                        metadata=metadata,
                        parse_artifact_sha256=parse_artifact_sha256,
                        canonicalizer_version=canonicalizer_version,
                    )
                )
                pending = []
            if table:
                chunks.append(
                    _make_chunk(
                        page,
                        [span],
                        metadata=metadata,
                        parse_artifact_sha256=parse_artifact_sha256,
                        canonicalizer_version=canonicalizer_version,
                    )
                )
            else:
                pending.append(span)
        if pending:
            chunks.append(
                _make_chunk(
                    page,
                    pending,
                    metadata=metadata,
                    parse_artifact_sha256=parse_artifact_sha256,
                    canonicalizer_version=canonicalizer_version,
                )
            )

    if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
        raise ValueError("canonicalization produced duplicate chunk IDs")
    for chunk in chunks:
        page = next(item for item in pages if item.page_idx == chunk.page_idx)
        if page.text[chunk.char_start : chunk.char_end] != chunk.text:
            raise ValueError("chunk character range does not restore its text")
    return pages, chunks


_INVOICE_NUMBER = re.compile(r"(?:票据号|票据编号|发票号码|发票号)\s*[:：]?\s*(?P<value>[A-Z0-9-]+)")
_AMOUNT = re.compile(r"(?P<value>\d+(?:\.\d{1,2})?)\s*元")
_OCCURRED_ON = re.compile(
    r"(?:票据日期|开票日期|合成票据)\s*[:：]?\s*"
    r"(?P<value>\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)"
)
_ROUTE = re.compile(
    r"(?P<value>(?:路线|行程|起止地点)\s*[:：]\s*[^\n。]{3,120}|"
    r"(?:起点|上车地点)\s*[:：]\s*[^；;，,。\n]+[；;，,]\s*"
    r"(?:终点|下车地点)\s*[:：]\s*[^\n。]{2,120})"
)
_CITY = re.compile(
    r"(?:城市\s*[:：]\s*|合成票据\s*[:：]\s*)"
    r"(?P<value>[\u4e00-\u9fff]{2,}(?:市|自治州|地区))"
)
_CHECK_IN = re.compile(r"(?P<value>\d{4}-\d{2}-\d{2})\s*入住")
_CHECK_OUT = re.compile(r"(?P<value>\d{4}-\d{2}-\d{2})\s*退房")
_ROOM_COUNT = re.compile(r"(?P<value>\d+)\s*间客房")
_ATTENDEE_COUNT = re.compile(r"(?:实际\s*)?(?:参加|用餐|参与)人数\s*[:：]?\s*(?P<value>\d+)\s*人")


# 字段归一化：仅去除首尾空白
def _identity(value: str) -> str:
    return value.strip()


# 字段归一化：把日期转为 ISO 8601 字符串
def _date_value(value: str) -> str:
    return date(*(int(part) for part in re.findall(r"\d+", value))).isoformat()


# 字段归一化：金额用 Decimal 校验有限非负后固定两位小数
def _amount_value(value: str) -> str:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid decimal amount")
    return format(amount, ".2f")


# 字段归一化：字符串转整数
def _integer_value(value: str) -> int:
    return int(value)


# 字段抽取规则：字段名、匹配正则与归一化函数的组合
FieldSpec = tuple[str, re.Pattern[str], Callable[[str], str | int]]
_FIELD_SPECS: dict[str, tuple[FieldSpec, ...]] = {
    "TAXI_INVOICE": (
        ("invoice_number", _INVOICE_NUMBER, _identity),
        ("amount", _AMOUNT, _amount_value),
        ("occurred_on", _OCCURRED_ON, _date_value),
        ("route", _ROUTE, _identity),
    ),
    "HOTEL_INVOICE": (
        ("invoice_number", _INVOICE_NUMBER, _identity),
        ("amount", _AMOUNT, _amount_value),
        ("city", _CITY, _identity),
        ("check_in", _CHECK_IN, _date_value),
        ("check_out", _CHECK_OUT, _date_value),
        ("room_count", _ROOM_COUNT, _integer_value),
    ),
    "ITINERARY": (
        ("check_in", _CHECK_IN, _date_value),
        ("check_out", _CHECK_OUT, _date_value),
    ),
    "MEAL_INVOICE": (
        ("invoice_number", _INVOICE_NUMBER, _identity),
        ("amount", _AMOUNT, _amount_value),
        ("occurred_on", _OCCURRED_ON, _date_value),
        ("attendee_count", _ATTENDEE_COUNT, _integer_value),
    ),
}


# 合并质量标记并去重，缺少 bbox 时补充 MISSING_BBOX
def _quality_flags(base: Sequence[str], bbox: BBox | None) -> list[str]:
    flags = list(dict.fromkeys(base))
    if bbox is None and "MISSING_BBOX" not in flags:
        flags.append("MISSING_BBOX")
    return flags


# 按证件类型用正则抽取冻结字段集，定位到页码/块号/bbox，并标记缺失或异常
def extract_document_fields(
    blocks: Iterable[ParsedBlock],
    document: Mapping[str, object],
    *,
    quality_flags: Sequence[str] = (),
) -> list[LocatedField]:
    """Extract the small, frozen set of fields required by the MVP fixtures."""

    document_id = _required_string(document, "document_id")
    document_type = _required_string(document, "document_type")
    try:
        specs = _FIELD_SPECS[document_type]
    except KeyError as exc:
        raise ValueError(f"unsupported attachment document_type: {document_type}") from exc

    ordered = sorted(blocks, key=lambda block: (block.page_idx, block.block_index))
    readable_blocks = [
        (block, _normalized_block_text(block))
        for block in ordered
        if block.kind.casefold() not in _AUXILIARY_KINDS
    ]
    readable_blocks = [(block, text) for block, text in readable_blocks if text]
    results: list[LocatedField] = []
    for field, pattern, normalize in specs:
        located: LocatedField | None = None
        for block, text in readable_blocks:
            match = pattern.search(text)
            if not match:
                continue
            raw_value = match.group("value")
            flags = _quality_flags(quality_flags, block.bbox)
            try:
                value = normalize(raw_value)
            except (ValueError, ArithmeticError):
                located = LocatedField(
                    field=field,
                    status=ExtractionStatus.UNREADABLE,
                    raw_value=raw_value,
                    document_id=document_id,
                    page=block.page_idx + 1,
                    block_index=block.block_index,
                    bbox=block.bbox,
                    quality_flags=[*flags, "NORMALIZATION_FAILED"],
                )
            else:
                located = LocatedField(
                    field=field,
                    status=ExtractionStatus.PRESENT,
                    value=value,
                    raw_value=raw_value,
                    document_id=document_id,
                    page=block.page_idx + 1,
                    block_index=block.block_index,
                    bbox=block.bbox,
                    quality_flags=flags,
                )
            break
        if located is None:
            no_text = not readable_blocks
            located = LocatedField(
                field=field,
                status=ExtractionStatus.UNREADABLE if no_text else ExtractionStatus.MISSING,
                document_id=document_id,
                quality_flags=list(
                    dict.fromkeys(
                        [*quality_flags, "NO_READABLE_TEXT" if no_text else "PATTERN_NOT_FOUND"]
                    )
                ),
            )
        results.append(located)
    by_name = {item.field: item for item in results}
    invoice = by_name.get("invoice_number")
    date_field = by_name.get("occurred_on") or by_name.get("check_out")
    if (
        invoice is not None
        and invoice.status == ExtractionStatus.PRESENT
        and date_field is not None
        and date_field.status == ExtractionStatus.PRESENT
    ):
        encoded_date = re.search(r"20\d{6}", str(invoice.value))
        expected_date = str(date_field.value).replace("-", "")
        if encoded_date and encoded_date.group() != expected_date:
            index = results.index(invoice)
            results[index] = LocatedField(
                field=invoice.field,
                status=ExtractionStatus.UNREADABLE,
                raw_value=invoice.raw_value,
                document_id=invoice.document_id,
                page=invoice.page,
                block_index=invoice.block_index,
                bbox=invoice.bbox,
                quality_flags=[*invoice.quality_flags, "CROSS_FIELD_DATE_MISMATCH"],
            )
    return results

"""Stable internal contracts for document ingestion."""

from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from app.models import DateOnly, ExtractionStatus, StrictModel

# 页内归一化矩形坐标：x0, y0, x1, y1，取值 0..1000
BBox = tuple[int, int, int, int]


# PDF 预检与解析状态机：READY 可直接解析，NEEDS_OCR 需走 OCR，其余为终态或错误
class ParseStatus(StrEnum):
    READY = "READY"
    NEEDS_OCR = "NEEDS_OCR"
    DONE = "DONE"
    EMPTY = "EMPTY"
    ENCRYPTED = "ENCRYPTED"
    TOO_LARGE = "TOO_LARGE"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    CORRUPT = "CORRUPT"
    LOW_TEXT_QUALITY = "LOW_TEXT_QUALITY"
    FAILED = "FAILED"


# 本地 PDF 预检结果：文件大小/页数/文本层质量，用于决定初始解析路线
class PdfPreflight(StrictModel):
    path: str = Field(min_length=1)
    status: ParseStatus
    file_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, strict=True)
    page_count: int | None = Field(default=None, ge=0, strict=True)
    native_text_chars: int = Field(default=0, ge=0, strict=True)
    suspicious_text_chars: int = Field(default=0, ge=0, strict=True)
    suggested_ocr: bool = False
    message: str | None = None

    # 校验 OCR 判定：NEEDS_OCR 必须建议 OCR，READY 不得建议 OCR
    @model_validator(mode="after")
    def check_ocr_status(self) -> "PdfPreflight":
        if self.status == ParseStatus.NEEDS_OCR and not self.suggested_ocr:
            raise ValueError("NEEDS_OCR preflight must suggest OCR")
        if self.status == ParseStatus.READY and self.suggested_ocr:
            raise ValueError("READY preflight cannot suggest OCR")
        return self


# MinerU 解析出的单个内容块（段落/表格/公式等），带页内归一化坐标
class ParsedBlock(StrictModel):
    page_idx: int = Field(ge=0, strict=True)
    block_index: int = Field(ge=0, strict=True)
    kind: str = Field(min_length=1)
    text: str = ""
    text_level: int | None = Field(default=None, ge=0, strict=True)
    bbox: BBox | None = None

    # 校验 bbox：坐标须归一化到 0..1000 且左下不得大于右上
    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, value: BBox | None) -> BBox | None:
        if value is None:
            return None
        x0, y0, x1, y1 = value
        if any(coordinate < 0 or coordinate > 1000 for coordinate in value):
            raise ValueError("bbox coordinates must be normalized to 0..1000")
        if x0 > x1 or y0 > y1:
            raise ValueError("bbox lower bounds cannot exceed upper bounds")
        return value


# 单页规范化文本：以块分隔符拼接，作为 chunk 字符偏移的基准
class PageContent(StrictModel):
    page_idx: int = Field(ge=0, strict=True)
    page_number: int = Field(ge=1, strict=True)
    text: str

    # 校验页码：page_number 必须等于 page_idx + 1
    @model_validator(mode="after")
    def check_page_number(self) -> "PageContent":
        if self.page_number != self.page_idx + 1:
            raise ValueError("page_number must equal page_idx + 1")
        return self


# 可入库的确定性文本块：携带字符范围、标题路径、来源哈希与制度过滤元数据
class DocumentChunk(StrictModel):
    chunk_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_id: str = Field(min_length=1)
    source_kind: Literal["POLICY", "ATTACHMENT"]
    title: str | None = None
    version: str = Field(min_length=1)
    page_idx: int = Field(ge=0, strict=True)
    page_number: int = Field(ge=1, strict=True)
    title_path: list[str] = Field(default_factory=list)
    char_start: int = Field(ge=0, strict=True)
    char_end: int = Field(gt=0, strict=True)
    text: str = Field(min_length=1)
    block_indices: list[int] = Field(min_length=1)
    bboxes: list[BBox] = Field(default_factory=list)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parse_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonicalizer_version: str = Field(min_length=1)

    catalog_snapshot_id: str | None = None
    published_status: str | None = None
    authority_level: str | None = None
    priority: int | None = Field(default=None, strict=True)
    supersedes_document_id: str | None = None
    department_ids: list[str] = Field(default_factory=list)
    expense_types: list[str] = Field(default_factory=list)
    effective_from: DateOnly | None = None
    effective_to: DateOnly | None = None
    fixture_key: str | None = None
    document_type: str | None = None
    media_type: str | None = None
    source_type: str | None = None
    reference_source_ids: list[str] = Field(default_factory=list)
    license_id: str | None = None
    synthetic: bool = False
    pdf_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    # 校验定位一致：页码连续、字符范围与正文等长、块索引唯一、生效区间有序
    @model_validator(mode="after")
    def check_location(self) -> "DocumentChunk":
        if self.page_number != self.page_idx + 1:
            raise ValueError("page_number must equal page_idx + 1")
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("character range length must equal chunk text length")
        if len(self.block_indices) != len(set(self.block_indices)):
            raise ValueError("chunk block indices must be unique")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective end cannot predate start")
        return self


# 从票据中抽取并定位到原文的字段：含页码/块号/bbox 与质量标记
class LocatedField(StrictModel):
    field: str = Field(min_length=1)
    status: ExtractionStatus
    value: str | int | bool | None = None
    raw_value: str | None = None
    document_id: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1, strict=True)
    block_index: int | None = Field(default=None, ge=0, strict=True)
    bbox: BBox | None = None
    quality_flags: list[str] = Field(default_factory=list)

    # 复用 ParsedBlock 的 bbox 校验规则
    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, value: BBox | None) -> BBox | None:
        return ParsedBlock.validate_bbox(value)

    # 校验字段状态与取值/来源一致：PRESENT 需值+定位，MISSING 不得带值，UNREADABLE 不得有归一化值
    @model_validator(mode="after")
    def check_value_and_source(self) -> "LocatedField":
        if len(self.quality_flags) != len(set(self.quality_flags)):
            raise ValueError("quality flags must be unique")
        if self.status == ExtractionStatus.PRESENT:
            if self.value is None or self.raw_value is None:
                raise ValueError("present field requires raw and normalized values")
            if self.page is None or self.block_index is None:
                raise ValueError("present field requires page and block source")
        elif self.status == ExtractionStatus.MISSING:
            if any(
                value is not None
                for value in (self.value, self.raw_value, self.page, self.block_index, self.bbox)
            ):
                raise ValueError("missing field cannot claim a value or source location")
        else:
            if self.value is not None:
                raise ValueError("unreadable field cannot have a normalized value")
            if self.raw_value is not None and (self.page is None or self.block_index is None):
                raise ValueError("unreadable raw value requires page and block source")
        return self

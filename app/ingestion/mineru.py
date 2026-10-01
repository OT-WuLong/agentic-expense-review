"""Minimal client for MinerU's signed-upload precision API."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx2
from pydantic import ValidationError

from app.ingestion.models import ParsedBlock

# httpx2 logs complete request URLs at INFO, including signed query parameters.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore2").setLevel(logging.WARNING)

_BASE_URL = "https://mineru.net/api/v4"
_UPLOAD_HOSTS = frozenset({"mineru.oss-cn-shanghai.aliyuncs.com"})
_RESULT_HOSTS = frozenset({"cdn-mineru.openxlab.org.cn"})
_ACTIVE_STATES = frozenset({"waiting-file", "pending", "running", "converting"})
_MAX_INPUT_BYTES = 200 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 4096
_MAX_COMPRESSION_RATIO = 500
_READ_CHUNK_BYTES = 64 * 1024
_RETRY_DELAYS = (1, 2)


# MinerU 客户端异常基类：错误信息不得包含凭证或带签名的 URL
class MinerUError(RuntimeError):
    """Base error that never includes credentials or signed URLs."""


# MinerU 返回了畸形或不受支持的响应
class MinerUProtocolError(MinerUError):
    """MinerU returned a malformed or unsupported response."""


# MinerU 任务到达 failed 终态
class MinerUTaskFailed(MinerUError):
    """MinerU reached the failed terminal state."""


# MinerU 在截止时间前未到达终态（轮询超时）
class MinerUTimeout(MinerUError):
    """MinerU did not reach a terminal state before the deadline."""


# 从 MinerU 结果压缩包中读取的可信子集：块列表、Markdown 与包哈希
@dataclass(frozen=True, slots=True)
class MinerUParseResult:
    """The small, trusted subset read from a MinerU result archive."""

    blocks: tuple[ParsedBlock, ...]
    markdown: str
    archive_sha256: str
    archive: bytes = field(repr=False)
    mineru_version: str | None = None
    backend: str | None = None


# MinerU 精解析 API 的同步单文件客户端：上传 → 轮询 → 下载
class MinerUClient:
    """Blocking one-file client for the MinerU precision API."""

    # 初始化客户端：校验 token 与各超时参数，token 仅保存在实例内
    def __init__(
        self,
        token: str,
        *,
        request_timeout_seconds: float = 300,
        poll_timeout_seconds: float = 900,
        poll_interval_seconds: float = 2,
    ) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("MinerU token is required")
        if min(request_timeout_seconds, poll_timeout_seconds, poll_interval_seconds) <= 0:
            raise ValueError("MinerU timeouts and poll interval must be positive")
        self._token = token.strip()
        self._request_timeout = request_timeout_seconds
        self._poll_timeout = poll_timeout_seconds
        self._poll_interval = poll_interval_seconds

    # 解析本地单个文件：上传、等待解析完成并在内存中读取结果包
    def parse_file(
        self,
        path: str | Path,
        *,
        data_id: str | None = None,
        is_ocr: bool = False,
        model_version: str = "pipeline",
    ) -> MinerUParseResult:
        """Upload one local file, wait for parsing, and read its in-memory bundle."""
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        file_size = file_path.stat().st_size
        if file_size <= 0:
            raise ValueError("MinerU input file is empty")
        if file_size > _MAX_INPUT_BYTES:
            raise ValueError("MinerU input exceeds the 200 MB API limit")
        if data_id is not None and (not isinstance(data_id, str) or not data_id):
            raise ValueError("data_id must be a non-empty string")
        if model_version not in {"pipeline", "vlm"}:
            raise ValueError("model_version must be 'pipeline' or 'vlm'")

        with httpx2.Client(timeout=self._request_timeout) as client:
            batch_id, upload_url = self._request_upload_url(
                client,
                file_path.name,
                data_id=data_id,
                is_ocr=is_ocr,
                model_version=model_version,
            )
            self._upload(client, upload_url, file_path, file_size)
            zip_url = self._poll(client, batch_id, file_path.name, data_id)
            bundle = self._download(client, zip_url)
        return read_mineru_bundle(bundle)

    # 构造鉴权头：按请求生成，避免凭证成为客户端默认头而泄漏到 OSS/CDN
    def _auth_headers(self) -> dict[str, str]:
        # Kept per request so credentials can never become client defaults and leak to OSS/CDN.
        return {"Authorization": f"Bearer {self._token}"}

    # 请求上传 URL：校验返回的 batch_id 与上传地址主机是否合法
    def _request_upload_url(
        self,
        client: httpx2.Client,
        file_name: str,
        *,
        data_id: str | None,
        is_ocr: bool,
        model_version: str,
    ) -> tuple[str, str]:
        file_spec: dict[str, object] = {"name": file_name, "is_ocr": is_ocr}
        if data_id is not None:
            file_spec["data_id"] = data_id
        payload = {
            "files": [file_spec],
            "model_version": model_version,
            "enable_table": True,
            "enable_formula": True,
            "language": "ch",
        }
        data = self._api_json(
            client,
            "POST",
            f"{_BASE_URL}/file-urls/batch",
            operation="upload URL request",
            json=payload,
        )
        batch_id = _required_string(data, "batch_id", "upload URL response")
        raw_urls = data.get("file_urls")
        if not isinstance(raw_urls, list) or len(raw_urls) != 1:
            raise MinerUProtocolError("MinerU upload URL response has invalid file_urls")
        upload_url = raw_urls[0]
        if not isinstance(upload_url, str) or not upload_url:
            raise MinerUProtocolError("MinerU upload URL response contains an invalid URL")
        _require_https_url(upload_url, "upload", _UPLOAD_HOSTS)
        return batch_id, upload_url

    # 上传文件字节：瞬时连接故障或服务端限流做有限重试
    def _upload(
        self,
        client: httpx2.Client,
        upload_url: str,
        path: Path,
        file_size: int,
    ) -> None:
        # These uploads are bounded to 200 MB. Bytes avoid flaky chunked/streaming PUTs while
        # keeping the provider's required absence of Content-Type.
        content = path.read_bytes()
        if len(content) != file_size:
            raise MinerUError("MinerU input changed while it was being uploaded")
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                response = client.put(
                    upload_url,
                    content=content,
                    headers={"Content-Length": str(file_size)},
                )
            except httpx2.HTTPError as exc:
                if attempt < len(_RETRY_DELAYS):
                    time.sleep(_RETRY_DELAYS[attempt])
                    continue
                raise MinerUError(
                    f"MinerU file upload failed ({type(exc).__name__})"
                ) from None
            if 200 <= response.status_code < 300:
                return
            if attempt < len(_RETRY_DELAYS) and (
                response.status_code in {408, 429} or response.status_code >= 500
            ):
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            raise MinerUError(f"MinerU file upload failed with HTTP {response.status_code}")

    # 轮询批次结果直到 done/failed，超时抛 MinerUTimeout，并校验结果包 URL
    def _poll(
        self,
        client: httpx2.Client,
        batch_id: str,
        file_name: str,
        data_id: str | None,
    ) -> str:
        deadline = time.monotonic() + self._poll_timeout
        while True:
            data = self._api_json(
                client,
                "GET",
                f"{_BASE_URL}/extract-results/batch/{batch_id}",
                operation="batch result request",
            )
            returned_batch_id = _required_string(data, "batch_id", "batch result response")
            if returned_batch_id != batch_id:
                raise MinerUProtocolError("MinerU batch result returned a different batch_id")
            result = _select_result(data.get("extract_result"), file_name, data_id)
            state = _required_string(result, "state", "batch item")
            if state == "done":
                zip_url = _required_string(result, "full_zip_url", "completed batch item")
                _require_https_url(zip_url, "result", _RESULT_HOSTS)
                return zip_url
            if state == "failed":
                raise MinerUTaskFailed("MinerU parsing failed")
            if state not in _ACTIVE_STATES:
                raise MinerUProtocolError("MinerU returned an unsupported task state")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MinerUTimeout("MinerU polling timed out")
            time.sleep(min(self._poll_interval, remaining))

    # 流式下载结果包，按声明大小与累计大小双重限制防止下载过大内容
    def _download(self, client: httpx2.Client, zip_url: str) -> bytes:
        for attempt in range(len(_RETRY_DELAYS) + 1):
            buffer = io.BytesIO()  # discard partial bytes before retrying the signed URL
            try:
                with client.stream("GET", zip_url, follow_redirects=False) as response:
                    if not 200 <= response.status_code < 300:
                        if attempt < len(_RETRY_DELAYS) and (
                            response.status_code in {408, 429} or response.status_code >= 500
                        ):
                            time.sleep(_RETRY_DELAYS[attempt])
                            continue
                        raise MinerUError(
                            f"MinerU result download failed with HTTP {response.status_code}"
                        )
                    declared_size = response.headers.get("Content-Length")
                    if (
                        declared_size
                        and declared_size.isdecimal()
                        and int(declared_size) > _MAX_ARCHIVE_BYTES
                    ):
                        raise MinerUProtocolError("MinerU result archive is too large")
                    for chunk in response.iter_bytes(_READ_CHUNK_BYTES):
                        if buffer.tell() + len(chunk) > _MAX_ARCHIVE_BYTES:
                            raise MinerUProtocolError("MinerU result archive is too large")
                        buffer.write(chunk)
            except httpx2.HTTPError as exc:
                if attempt < len(_RETRY_DELAYS):
                    time.sleep(_RETRY_DELAYS[attempt])
                    continue
                raise MinerUError(
                    f"MinerU result download failed ({type(exc).__name__})"
                ) from None
            return buffer.getvalue()
        raise AssertionError("unreachable MinerU download retry state")

    # 调用 MinerU JSON API：校验 code=0，仅瞬时故障重试且不透出原始响应
    def _api_json(
        self,
        client: httpx2.Client,
        method: str,
        url: str,
        *,
        operation: str,
        json: object | None = None,
    ) -> Mapping[str, object]:
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                response = client.request(
                    method,
                    url,
                    headers=self._auth_headers(),
                    json=json,
                )
            except httpx2.HTTPError as exc:
                if attempt < len(_RETRY_DELAYS):
                    time.sleep(_RETRY_DELAYS[attempt])
                    continue
                raise MinerUError(
                    f"MinerU {operation} failed ({type(exc).__name__})"
                ) from None
            if 200 <= response.status_code < 300:
                break
            if attempt < len(_RETRY_DELAYS) and (
                response.status_code in {408, 429} or response.status_code >= 500
            ):
                time.sleep(_RETRY_DELAYS[attempt])
                continue
            raise MinerUError(f"MinerU {operation} failed with HTTP {response.status_code}")
        else:  # pragma: no cover - both retry paths terminate above.
            raise MinerUError(f"MinerU {operation} failed")
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise MinerUProtocolError(f"MinerU {operation} returned invalid JSON") from None
        if not isinstance(payload, Mapping):
            raise MinerUProtocolError(f"MinerU {operation} returned a non-object response")
        code = payload.get("code")
        if type(code) is not int:  # bool is intentionally rejected
            raise MinerUProtocolError(f"MinerU {operation} response has invalid code")
        if code != 0:
            raise MinerUError(f"MinerU {operation} failed with API code {code}")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise MinerUProtocolError(f"MinerU {operation} response has invalid data")
        return data


# 在批次结果中按 data_id 优先、file_name 兜底，唯一定位目标文件
def _select_result(
    value: object,
    file_name: str,
    data_id: str | None,
) -> Mapping[str, object]:
    if not isinstance(value, list) or not value:
        raise MinerUProtocolError("MinerU batch result has invalid extract_result")
    items: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise MinerUProtocolError("MinerU batch result contains a non-object item")
        items.append(item)
    matches = [item for item in items if data_id is not None and item.get("data_id") == data_id]
    if not matches:
        matches = [item for item in items if item.get("file_name") == file_name]
    if len(matches) != 1:
        raise MinerUProtocolError("MinerU batch result does not identify exactly one file")
    return matches[0]


# 读取必填非空字符串字段，否则抛协议错误
def _required_string(value: Mapping[str, object], key: str, context: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise MinerUProtocolError(f"MinerU {context} has invalid {key}")
    return item


# 校验 URL：必须为 https、主机在白名单内、且不含用户信息与非 443 端口
def _require_https_url(value: str, purpose: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise MinerUProtocolError(f"MinerU returned an invalid {purpose} URL") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise MinerUProtocolError(f"MinerU returned an invalid {purpose} URL")


# 校验并适配缓存的 MinerU 结果包：解压安全校验后读取 content_list.json 与 full.md
def read_mineru_bundle(data: bytes) -> MinerUParseResult:
    """Validate and adapt a cached MinerU result archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            _validate_archive_members(members)
            content_info = _one_member(
                members,
                lambda name: name == "content_list.json" or name.endswith("_content_list.json"),
                "content list",
            )
            markdown_info = _one_member(members, lambda name: name == "full.md", "full.md")
            raw_content = _read_member(archive, content_info)
            raw_markdown = _read_member(archive, markdown_info)
            version, backend = _read_optional_version(archive, members)
    except zipfile.BadZipFile:
        raise MinerUProtocolError("MinerU result is not a valid ZIP archive") from None

    try:
        content_list = json.loads(raw_content.decode("utf-8"))
        markdown = raw_markdown.decode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MinerUProtocolError("MinerU result contains invalid UTF-8 or JSON") from None
    return MinerUParseResult(
        blocks=tuple(_content_list_to_blocks(content_list)),
        markdown=markdown,
        archive_sha256=hashlib.sha256(data).hexdigest(),
        archive=data,
        mineru_version=version,
        backend=backend,
    )


# 校验压缩包成员：防路径穿越、加密成员、超大体积与异常压缩比
def _validate_archive_members(members: list[zipfile.ZipInfo]) -> None:
    if not members or len(members) > _MAX_ARCHIVE_MEMBERS:
        raise MinerUProtocolError("MinerU result archive has an invalid member count")
    total_size = 0
    for member in members:
        normalized = member.filename.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or "\x00" in normalized
            or path.is_absolute()
            or ".." in path.parts
            or (path.parts and path.parts[0].endswith(":"))
        ):
            raise MinerUProtocolError("MinerU result archive contains an unsafe path")
        if member.flag_bits & 0x1:
            raise MinerUProtocolError("MinerU result archive contains an encrypted member")
        if member.file_size < 0 or member.compress_size < 0:
            raise MinerUProtocolError("MinerU result archive contains an invalid member")
        total_size += member.file_size
        if total_size > _MAX_UNCOMPRESSED_BYTES:
            raise MinerUProtocolError("MinerU result archive expands beyond the safety limit")
        if member.file_size > _MAX_MEMBER_BYTES:
            raise MinerUProtocolError("MinerU result archive member is too large")
        if member.file_size and member.file_size / max(member.compress_size, 1) > _MAX_COMPRESSION_RATIO:
            raise MinerUProtocolError("MinerU result archive has a suspicious compression ratio")


# 按文件名谓词取唯一成员，数量不为 1 时抛协议错误
def _one_member(
    members: list[zipfile.ZipInfo],
    predicate: Any,
    label: str,
) -> zipfile.ZipInfo:
    matches = [member for member in members if predicate(PurePosixPath(member.filename).name)]
    if len(matches) != 1:
        raise MinerUProtocolError(f"MinerU result archive must contain exactly one {label}")
    return matches[0]


# 读取单个成员内容，并再次限制解压后大小
def _read_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bytes:
    with archive.open(member) as source:
        data = source.read(_MAX_MEMBER_BYTES + 1)
    if len(data) > _MAX_MEMBER_BYTES:
        raise MinerUProtocolError("MinerU result archive member is too large")
    return data


# 从 layout.json 或 *_middle.json 读取可选的 MinerU 版本与后端标识
def _read_optional_version(
    archive: zipfile.ZipFile,
    members: list[zipfile.ZipInfo],
) -> tuple[str | None, str | None]:
    candidates = [
        member for member in members if PurePosixPath(member.filename).name == "layout.json"
    ]
    if not candidates:
        candidates = [
            member
            for member in members
            if PurePosixPath(member.filename).name.endswith("_middle.json")
        ]
    if len(candidates) != 1:
        return None, None
    try:
        metadata = json.loads(_read_member(archive, candidates[0]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(metadata, Mapping):
        return None, None
    version = metadata.get("_version_name")
    backend = metadata.get("_backend")
    return (
        version if isinstance(version, str) and version else None,
        backend if isinstance(backend, str) and backend else None,
    )


# 把 content_list.json 转为 ParsedBlock 列表，跳过无关类型与空文本块
def _content_list_to_blocks(value: object) -> list[ParsedBlock]:
    if not isinstance(value, list):
        raise MinerUProtocolError("MinerU content list must be an array")
    blocks: list[ParsedBlock] = []
    for block_index, raw_block in enumerate(value):
        if not isinstance(raw_block, Mapping):
            raise MinerUProtocolError("MinerU content list contains a non-object block")
        kind = raw_block.get("type")
        if not isinstance(kind, str) or not kind:
            raise MinerUProtocolError("MinerU content block has invalid type")
        if kind not in {"text", "table", "equation", "list", "image"}:
            continue
        page_idx = _required_non_negative_int(raw_block, "page_idx")
        bbox = _bbox(raw_block.get("bbox"))
        text_level = _text_level(raw_block.get("text_level"))
        text = _block_text(raw_block, kind)
        if not text.strip():
            continue
        try:
            blocks.append(
                ParsedBlock(
                    page_idx=page_idx,
                    block_index=block_index,
                    kind=kind,
                    text=text,
                    text_level=text_level,
                    bbox=bbox,
                )
            )
        except ValidationError:
            raise MinerUProtocolError("MinerU content block failed validation") from None
    return blocks


# 按块类型抽取文本：文本/公式取 text，列表取 list_items，表格拼接标题/正文/脚注
def _block_text(block: Mapping[str, object], kind: str) -> str:
    if kind in {"text", "equation"}:
        return _required_text(block, "text", kind)
    if kind == "list":
        return "\n".join(_string_list(block, "list_items", required=True))
    if kind == "image":
        return "\n".join(_string_list(block, "image_caption", required=False))
    parts = [
        *_string_list(block, "table_caption", required=False),
        _required_text(block, "table_body", "table"),
        *_string_list(block, "table_footnote", required=False),
    ]
    return "\n".join(part for part in parts if part)


# 读取必填字符串字段，否则抛协议错误
def _required_text(block: Mapping[str, object], key: str, kind: str) -> str:
    value = block.get(key)
    if not isinstance(value, str):
        raise MinerUProtocolError(f"MinerU {kind} block has invalid {key}")
    return value


# 读取字符串列表字段，required=False 时缺失返回空列表
def _string_list(
    block: Mapping[str, object],
    key: str,
    *,
    required: bool,
) -> list[str]:
    value = block.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise MinerUProtocolError(f"MinerU content block has invalid {key}")
    return value


# 读取必填非负整数字段（严格拒绝 bool）
def _required_non_negative_int(block: Mapping[str, object], key: str) -> int:
    value = block.get(key)
    if type(value) is not int or value < 0:
        raise MinerUProtocolError(f"MinerU content block has invalid {key}")
    return value


# 解析可选的标题层级，非法时抛协议错误
def _text_level(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise MinerUProtocolError("MinerU content block has invalid text_level")
    return value


# 解析 bbox：必须为 4 个整数坐标
def _bbox(value: object) -> tuple[int, int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise MinerUProtocolError("MinerU content block has invalid bbox")
    if any(type(coordinate) is not int for coordinate in value):
        raise MinerUProtocolError("MinerU content block has invalid bbox")
    return value[0], value[1], value[2], value[3]

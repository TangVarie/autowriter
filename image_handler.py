"""
Image handling for XHS Content Workstation.

Responsibilities:
  - Accept uploaded images (from Streamlit file_uploader)
  - Compress / resize to stay within API token limits
  - Encode to base64 for Claude/Gemini vision APIs
  - Upload to Supabase Storage and return public URLs
"""

from __future__ import annotations

import base64
import io
import os
import re
from typing import Optional

from PIL import Image
import streamlit as st
from supabase import Client

import config

# 解压缩炸弹防护(R-040 收紧): 旧值 100M 比 PIL 默认(~89M)还高 —— 注释声称
# "默认偏大"却实际放松了防线。主流程图最大 4096×4096 ≈ 16.7M 像素, 取 40M
# 已是 2.4 倍余量; 100M 像素 RGBA 解压 ≈ 400MB 内存, 几张并发即可打挂进程。
# 超上限时 PIL 抛 ``Image.DecompressionBombError``, compress_image 转成可读异常。
Image.MAX_IMAGE_PIXELS = 40_000_000

# R-040: 解压前的第一道闸 —— 原始字节上限。高压缩比炸弹几 KB 就能顶满像素
# 上限, 像素闸只防"解压后", 不防"读取与 base64 放大"(b64 +33%)。
MAX_RAW_IMAGE_BYTES = 20 * 1024 * 1024

# Max dimension per side (pixels) — keeps images under ~1000 tokens each
MAX_DIM = config.MAX_IMAGE_DIMENSION
SUPPORTED_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}

# 反向映射：MIME → 默认扩展名，用于原始文件名没有后缀或后缀对不上时兜底
_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png":  "png",
    "image/webp": "webp",
}


def _safe_storage_name(filename: str, mime_type: Optional[str] = None) -> str:
    """构造一个对 Supabase Storage 安全且不重复的对象名。

    - 去掉路径分隔符 / 反斜杠以防越权写到其它前缀
    - 仅保留 [A-Za-z0-9._-]，其它字符替换为 ``_``（含中文）
    - 追加 8-hex 随机后缀防止同名冲突
    - 没有合法扩展名时按 mime_type 兜底（推断 jpg/png/webp，否则 .bin）

    例：``"产品图.jpg"`` + 随机 → ``"____.a1b2c3d4.jpg"``。原始 name 保留在
    metadata.name 字段供 UI 展示，对象名只是存储 key。
    """
    raw = (filename or "").replace("/", "_").replace("\\", "_").strip() or "file"
    # 扩展名只认 1-6 位字母数字（典型 .jpg / .jpeg / .webp / .heic）。否则
    # 把整个 raw 当 stem，避免诸如 "../../etc/passwd" 被 rsplit 当成
    # ext="_etc_passwd" 这种荒谬切分。
    _ext_match = re.search(r"\.([A-Za-z0-9]{1,6})$", raw)
    if _ext_match:
        ext = _ext_match.group(1).lower()
        stem = raw[: _ext_match.start()]
    else:
        ext = ""
        stem = raw
    # 仅保留安全字符；不允许的字符（含中文）转为 _，多个连续 _ 收敛
    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem).strip("._") or "file"
    safe_stem = re.sub(r"_{2,}", "_", safe_stem)[:64]
    # 扩展名兜底：从 SUPPORTED_MIME 反查；都没有就按 mime 推断；最后落到 bin
    if ext not in SUPPORTED_MIME:
        ext = _MIME_TO_EXT.get((mime_type or "").lower(), "")
        if not ext and mime_type and mime_type.startswith("image/"):
            ext = mime_type.split("/", 1)[1].lower()
    suffix = os.urandom(4).hex()
    if ext:
        return f"{safe_stem}.{suffix}.{ext}"
    return f"{safe_stem}.{suffix}"


# ── Core compression & encoding ────────────────────────────────────────────

def compress_image(raw_bytes: bytes, max_dim: int = MAX_DIM) -> tuple[bytes, str]:
    """
    Resize the image so neither dimension exceeds max_dim.
    Returns (compressed_bytes, mime_type).

    Raises ``ValueError`` 时调用方应捕获并 surface 给 UI——避免一张异常图把
    整个 Streamlit 进程拖死。
    """
    # max_dim 必须是正整数；外部允许通过环境变量配置，错配（0/负数）会让
    # 下面的 ratio 计算除零或返回 inf。这里兜底到一个合理默认值。
    if not isinstance(max_dim, int) or max_dim <= 0:
        max_dim = 1568
    # R-040: 字节级上限先于解压(所有图片路径都经本函数, 单点设防)
    if raw_bytes and len(raw_bytes) > MAX_RAW_IMAGE_BYTES:
        raise ValueError(
            f"图片文件过大：{len(raw_bytes) / 1024 / 1024:.1f}MB "
            f"> 上限 {MAX_RAW_IMAGE_BYTES // 1024 // 1024}MB"
        )
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        # 强制立即 load 一次：``Image.open`` 是 lazy 的，bomb 要等到 resize 时
        # 才触发；这里提前触发以便在统一 try/except 里捕获。
        img.load()
    except Image.DecompressionBombError as exc:
        raise ValueError(f"图片像素数超过安全上限：{exc}") from exc

    # Determine output format
    fmt = img.format or "JPEG"
    mime = f"image/{fmt.lower()}"
    if fmt.lower() in ("jpg", "jpeg"):
        mime = "image/jpeg"
        save_fmt = "JPEG"
    elif fmt.lower() == "png":
        save_fmt = "PNG"
    elif fmt.lower() == "webp":
        save_fmt = "WEBP"
    else:
        # Convert anything else to JPEG
        save_fmt = "JPEG"
        mime = "image/jpeg"
        img = img.convert("RGB")

    # Resize if needed
    w, h = img.size
    if w > max_dim or h > max_dim:
        ratio = min(max_dim / w, max_dim / h)
        new_w, new_h = int(w * ratio), int(h * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    buf = io.BytesIO()
    if save_fmt == "JPEG":
        img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=85, optimize=True)
    else:
        img.save(buf, format=save_fmt, optimize=True)

    return buf.getvalue(), mime


def encode_image_b64(raw_bytes: bytes, max_dim: int = MAX_DIM) -> dict:
    """
    Compress and base64-encode an image.

    Returns a dict compatible with Claude's vision API:
        {"data": "<b64>", "media_type": "image/jpeg"}
    """
    compressed, mime = compress_image(raw_bytes, max_dim)
    b64 = base64.standard_b64encode(compressed).decode("utf-8")
    return {"data": b64, "media_type": mime}


def encode_images(files) -> list[dict]:
    """
    Accept a list of Streamlit UploadedFile objects and return a list of
    encoded image dicts ready for the AI API.
    """
    encoded = []
    for f in (files or []):
        ext = f.name.rsplit(".", 1)[-1].lower()
        if ext not in SUPPORTED_MIME:
            continue
        raw = f.read()
        encoded.append(encode_image_b64(raw))
    return encoded


# ── Supabase Storage upload ────────────────────────────────────────────────

def upload_image_to_storage(
    client: Client,
    project_id: str,
    filename: str,
    raw_bytes: bytes,
    mime_type: str,
    bucket: str = "reference-files",
) -> str:
    """
    Upload image to Supabase Storage and return the public URL.

    对象名通过 ``_safe_storage_name`` 转义并追加随机后缀，避免同名冲突触发
    Supabase Storage 的 409 Duplicate，且不暴露原始路径字符（/ \\）。
    """
    compressed, mime = compress_image(raw_bytes)
    storage_name = _safe_storage_name(filename, mime_type=mime)
    path = f"projects/{project_id}/images/{storage_name}"
    client.storage.from_(bucket).upload(
        path,
        compressed,
        file_options={"content-type": mime},
    )
    return client.storage.from_(bucket).get_public_url(path)


# ── Streamlit upload widget ────────────────────────────────────────────────

def render_image_uploader(label: str = "上传参考图片（可选）") -> list[dict]:
    """
    Render a Streamlit file uploader and return encoded images.

    Returns a list of base64-encoded image dicts for direct use with
    Claude / Gemini vision APIs.
    """
    files = st.file_uploader(
        label,
        type=list(SUPPORTED_MIME.keys()),
        accept_multiple_files=True,
        key="generation_image_upload",
    )
    if not files:
        return []

    encoded = []
    for f in files:
        raw = f.read()
        try:
            img_dict = encode_image_b64(raw)
            encoded.append(img_dict)
            # Show a thumbnail
            st.image(f, caption=f.name, width=120)
        except Exception as e:
            st.warning(f"图片处理失败：{f.name} — {e}")

    return encoded


# ── Image URL → base64 for display in iterations ──────────────────────────

def url_to_b64(url: str) -> Optional[dict]:
    """
    Fetch an image from a URL and encode it to base64.
    Returns None on failure.
    """
    import requests
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return encode_image_b64(resp.content)
    except Exception:
        return None

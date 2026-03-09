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
from typing import Optional

from PIL import Image
import streamlit as st
from supabase import Client

import config

# Max dimension per side (pixels) — keeps images under ~1000 tokens each
MAX_DIM = config.MAX_IMAGE_DIMENSION
SUPPORTED_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


# ── Core compression & encoding ────────────────────────────────────────────

def compress_image(raw_bytes: bytes, max_dim: int = MAX_DIM) -> tuple[bytes, str]:
    """
    Resize the image so neither dimension exceeds max_dim.
    Returns (compressed_bytes, mime_type).
    """
    img = Image.open(io.BytesIO(raw_bytes))

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
    """
    compressed, mime = compress_image(raw_bytes)
    path = f"projects/{project_id}/images/{filename}"
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

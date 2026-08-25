"""图片压缩这一路用到的 Pillow API，用真图跑一遍。

存在的理由是**依赖升级**（跨库审计 SUP-017）：Pillow 卡在 11.3.0 是因为
``requirements.txt`` 写着 ``<12.0``，而已知漏洞的修复线在 12.2/12.3——40 条
通告里有 24 条是它的，而它正好是处理用户上传图片的那一层。

跨 major 升级要能被**验证**而不是靠读 changelog。这里逐个钉住实际用到的
API：``Image.open`` / ``MAX_IMAGE_PIXELS`` / ``DecompressionBombError`` /
``convert`` / ``resize(LANCZOS)`` / ``save``——尤其 ``Image.LANCZOS``，它在
Pillow 10 时把常量搬进了 ``Image.Resampling``，只留了别名；别名哪天没了，
压缩会在运行期炸而不是 import 期。
"""

from __future__ import annotations

import io

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image                       # noqa: E402

import image_handler as IH                  # noqa: E402


def _png(w: int, h: int, color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def test_lanczos_constant_still_exists():
    """``Image.LANCZOS`` 是 Pillow 10 之后保留的别名，compress_image 直接在用。

    它没了的话是**运行期**炸（压一张图的时候），不是 import 期——所以值得
    单独钉一条，而不是等某次真实上传去发现。
    """
    assert hasattr(Image, "LANCZOS"), (
        "Image.LANCZOS 没了 —— image_handler.compress_image 会在运行期炸, "
        "改用 Image.Resampling.LANCZOS")


def test_small_image_round_trips():
    """返回的第二个值是 **mime type**(image/png), 不是 Pillow 的格式名。"""
    raw = _png(64, 48)
    out, mime = IH.compress_image(raw, max_dim=1024)
    assert out and mime.startswith("image/"), mime
    back = Image.open(io.BytesIO(out))
    assert back.size == (64, 48), back.size


def test_oversized_image_is_downscaled_preserving_aspect():
    raw = _png(2000, 1000)
    out, _fmt = IH.compress_image(raw, max_dim=500)
    back = Image.open(io.BytesIO(out))
    assert max(back.size) <= 500, back.size
    # 2:1 的长宽比要保住（允许一个像素的取整误差）
    assert abs(back.size[0] / back.size[1] - 2.0) < 0.02, back.size


def test_pixel_bomb_is_rejected_by_header_not_by_decoding():
    """超 MAX_IMAGE_PIXELS 要在**解码前**按 header 尺寸拦下。

    Pillow 自己的语义是超限只发 DecompressionBombWarning、照常解码，所以
    image_handler 在 ``Image.open`` 之后显式按 w*h 拦了一道。这条盯的就是
    那一道还在。
    """
    monkey = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = 1000          # 32×32 就超
        with pytest.raises(ValueError):
            IH.compress_image(_png(64, 64))
    finally:
        Image.MAX_IMAGE_PIXELS = monkey


def test_byte_size_cap_is_enforced():
    """超 MAX_RAW_IMAGE_BYTES 直接拒，别先解码再说。"""
    with pytest.raises(ValueError):
        IH.compress_image(b"x" * (IH.MAX_RAW_IMAGE_BYTES + 1))


def test_garbage_bytes_raise_instead_of_returning_something():
    """不是图片的字节必须**抛**，不能悄悄返回一份"结果"。

    ⚠️ 这里刻意只断言"会抛"，不断言异常类型: Pillow 抛的是
    ``UnidentifiedImageError``(OSError 的子类)，而唯一的调用点
    ``render_image_uploader`` 是 ``except Exception`` + 一条用户可见的
    warning——已经兜住了。硬要求 ValueError 是我一开始写窄了，那会把一条
    正常工作的路径判成 bug。真正不能接受的是"返回了", 那才会让一段垃圾
    字节当成图片发给模型。
    """
    with pytest.raises(Exception):
        IH.compress_image(b"definitely not an image")


def test_rgba_is_flattened_for_jpeg():
    """带 alpha 的图存 JPEG 前必须 convert("RGB")，否则 Pillow 直接抛。"""
    buf = io.BytesIO()
    Image.new("RGBA", (40, 40), (10, 20, 30, 128)).save(buf, format="PNG")
    out, _fmt = IH.compress_image(buf.getvalue(), max_dim=1024)
    assert out
    assert Image.open(io.BytesIO(out)).mode in ("RGB", "RGBA", "P")

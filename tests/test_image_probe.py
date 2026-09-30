"""验证「用实测像素代替猜变换段命名」（#8）。

背景：原判定只认 ``~tplv-dy-shrink:`` 这一个字符串（从命名反推）。
抖音换个变体名就失效，而且失效方式是**静默选到低清**——不报错、界面还标着
「高清原图」。现在改成量真实像素：取图片头部若干字节解析宽高，再和接口声明的
尺寸对照。

本测试分三层：
  A. 图片头解析（纯字节，无网络）——JPEG / PNG / GIF / WebP
  B. 判定逻辑（打桩实测结果）——缩略档被识破、原图不被误杀、探测失败安全回退
  C. 真实网络（可选）——若环境提供 DOUYIN_TEST_IMG，则真跑一次 Range 探测

运行：
    python tests/test_image_probe.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import struct
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "grabber"))

def _module_available(name: str) -> bool:
    """模块是否真的可用（不抛异常）。

    不能直接用 find_spec：若 `sys.modules[name]` 已被换成没有 `__spec__` 的桩，
    find_spec 会抛 `ValueError: <name>.__spec__ is None`。
    """
    mod = sys.modules.get(name)
    if mod is not None:
        return getattr(mod, "__spec__", None) is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# 只在**真的没装**时才打桩。
# 注意不能用 `if "httpx" not in sys.modules`：在导入 parsers 之前 httpx 还没被导入，
# 那个判断会把桩装上，于是即使环境里有真 httpx，measure_image_size 也会走到假客户端
# （这个坑让本文件的真实网络测试一开始误报失败）。
if not _module_available("httpx"):
    _s = types.ModuleType("httpx")
    class _C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    _s.AsyncClient = _C
    _s.__spec__ = None
    sys.modules["httpx"] = _s
if not _module_available("gmssl"):
    _g = types.ModuleType("gmssl"); _g.sm3 = types.ModuleType("gmssl.sm3"); _g.func = types.ModuleType("gmssl.func")
    _g.sm3.sm3_hash = lambda *a, **k: ""
    sys.modules["gmssl"] = _g; sys.modules["gmssl.sm3"] = _g.sm3; sys.modules["gmssl.func"] = _g.func

import parsers.base as B  # noqa: E402

_ok = _bad = 0


def check(label, cond, extra=""):
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"  PASS  {label}")
    else:
        _bad += 1
        print(f"  FAIL  {label}  {extra}")


# ── 构造各格式的最小头部 ──────────────────────────────────────────────
def jpeg_header(w: int, h: int, with_exif: bool = False) -> bytes:
    out = bytearray(b"\xff\xd8")
    if with_exif:
        # 造一个较大的 APP1/EXIF 段，验证解析器会正确跳过它
        payload = b"Exif\x00\x00" + b"\x00" * 4000
        out += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    # SOF0
    out += b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", h, w) + b"\x03" + b"\x00" * 9
    out += b"\xff\xd9"
    return bytes(out)


def png_header(w: int, h: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00" + b"\x00" * 8)


def gif_header(w: int, h: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 8


def webp_vp8x_header(w: int, h: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 30) + b"WEBP" + b"VP8X"
            + b"\x00" * 8 + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little"))


def main() -> int:
    print("=" * 76)
    print("A) 图片头解析（纯字节，无网络）")
    print("=" * 76)
    check("JPEG 2160×3838", B.image_dimensions_from_bytes(jpeg_header(2160, 3838)) == (2160, 3838),
          str(B.image_dimensions_from_bytes(jpeg_header(2160, 3838))))
    check("JPEG 跳过大的 EXIF 段",
          B.image_dimensions_from_bytes(jpeg_header(3024, 4032, with_exif=True)) == (3024, 4032),
          str(B.image_dimensions_from_bytes(jpeg_header(3024, 4032, with_exif=True))))
    check("PNG 1440×2559", B.image_dimensions_from_bytes(png_header(1440, 2559)) == (1440, 2559),
          str(B.image_dimensions_from_bytes(png_header(1440, 2559))))
    check("GIF 320×240", B.image_dimensions_from_bytes(gif_header(320, 240)) == (320, 240),
          str(B.image_dimensions_from_bytes(gif_header(320, 240))))
    check("WebP(VP8X) 2160×3838",
          B.image_dimensions_from_bytes(webp_vp8x_header(2160, 3838)) == (2160, 3838),
          str(B.image_dimensions_from_bytes(webp_vp8x_header(2160, 3838))))
    check("非图片字节返回 None", B.image_dimensions_from_bytes(b"not an image at all") is None)
    check("空字节返回 None", B.image_dimensions_from_bytes(b"") is None)
    check("截断的 JPEG 不抛异常", B.image_dimensions_from_bytes(b"\xff\xd8\xff\xc0\x00") is None)

    print()
    print("=" * 76)
    print("B) 候选排序：命名层就能挡掉明显的缩略档")
    print("=" * 76)
    HOST_A = "https://p3-pc-sign.douyinpic.com/tos-cn-i-0813/OBJKEY"
    HOST_B = "https://p9-pc-sign.douyinpic.com/tos-cn-i-0813/OBJKEY"
    img_mixed = {
        "width": 1440, "height": 2559,
        "url_list": [
            HOST_A + "~tplv-dy-shrink:1440:2559:q75.webp",   # 命名缩略
            HOST_A + "~tplv-dy-aweme-images:q75.webp",        # 原尺寸 webp
            HOST_A + "~tplv-dy-aweme-images:q75.jpeg",        # 原尺寸 jpeg ← 首选
            HOST_B + "~tplv-dy-aweme-images:q75.jpeg",
        ],
    }
    cands = B.douyin_image_candidates(img_mixed)
    check("候选非空", bool(cands), str(cands))
    check("首选是原尺寸 jpeg", cands and "aweme-images:q75.jpeg" in cands[0], cands[0] if cands else "")
    check("命名的缩略档排在最后", cands and "dy-shrink" in cands[-1], cands[-1] if cands else "")

    # 换一个**平台改名**后的缩略档：旧逻辑完全认不出，新逻辑靠「写死尺寸」能识别
    img_renamed = {
        "width": 1440, "height": 2559,
        "url_list": [
            HOST_A + "~tplv-resize:1440:2559:q75.jpeg",   # 改名了，但尺寸写死
            HOST_A + "~tplv-dy-aweme-images:q75.jpeg",
        ],
    }
    cands2 = B.douyin_image_candidates(img_renamed)
    check("改名后的缩略档被排到后面（靠写死尺寸识别）",
          cands2 and "tplv-dy-aweme-images" in cands2[0] and "resize" in cands2[-1],
          str(cands2))

    print()
    print("=" * 76)
    print("B2) 变体名归一化：同档的 webp/jpeg 不能算两种画质")
    print("=" * 76)
    check("同档不同格式 → 同名",
          B._dy_variant_name(HOST_A + "~tplv-dy-aweme-images:q75.webp")
          == B._dy_variant_name(HOST_A + "~tplv-dy-aweme-images:q75.jpeg"),
          B._dy_variant_name(HOST_A + "~tplv-dy-aweme-images:q75.webp") + " vs "
          + B._dy_variant_name(HOST_A + "~tplv-dy-aweme-images:q75.jpeg"))
    check("同档不同主机 → 同名",
          B._dy_variant_name(HOST_A + "~tplv-dy-aweme-images:q75.jpeg")
          == B._dy_variant_name(HOST_B + "~tplv-dy-aweme-images:q75.jpeg"))
    check("shrink 与 aweme-images → 不同名",
          B._dy_variant_name(HOST_A + "~tplv-dy-shrink:1440:2559:q75.webp")
          != B._dy_variant_name(HOST_A + "~tplv-dy-aweme-images:q75.jpeg"))
    check("改名 resize 与 aweme-images → 不同名",
          B._dy_variant_name(HOST_A + "~tplv-resize:1440:2559:q75.jpeg")
          != B._dy_variant_name(HOST_A + "~tplv-dy-aweme-images:q75.jpeg"))
    check("无变换段时回退为整串", B._dy_variant_name("https://x/y.jpg") == "https://x/y.jpg")

    print()
    print("=" * 76)
    print("B3) 单档位（常见形态）不应触发探测；多档位才探测")
    print("=" * 76)
    from parsers.web_api import _pick_image_verified  # noqa: E402
    import parsers.web_api as WA  # noqa: E402

    called = []
    real_measure = B.measure_image_size

    async def spy_measure(url, **kw):
        called.append(url)
        return await real_measure(url, **kw)

    WA.measure_image_size = spy_measure
    try:
        # 单一档位：webp + jpeg 是同一档的两种编码（变体名相同）→ 不该探测
        single = {
            "width": 2160, "height": 3838,
            "url_list": [HOST_A + "~tplv-dy-aweme-images:q75.webp",
                         HOST_A + "~tplv-dy-aweme-images:q75.jpeg",
                         HOST_B + "~tplv-dy-aweme-images:q75.jpeg"],
        }
        picked, note, alt = asyncio.run(_pick_image_verified(single))
        check("单档位不触发探测", called == [], f"却探测了 {called}")
        check("单档位仍选出原尺寸 jpeg", "aweme-images:q75.jpeg" in picked, picked)
        check("单档位无说明", note == "", note)
        check("备用档仍是同档其它主机",
              bool(alt) and "aweme-images" in alt, str(alt))

        # 多档位（含命名缩略档）→ 应该探测
        called.clear()
        multi = {
            "width": 1440, "height": 2559,
            "url_list": [HOST_A + "~tplv-dy-shrink:1440:2559:q75.webp",
                         HOST_A + "~tplv-dy-aweme-images:q75.webp",
                         HOST_A + "~tplv-dy-aweme-images:q75.jpeg"],
        }
        asyncio.run(_pick_image_verified(multi))
        check("多档位触发探测（每档只探一次）", len(called) == 2, f"探测了 {len(called)} 次: {called}")
    finally:
        WA.measure_image_size = real_measure

    print()
    print("=" * 76)
    print("C) 判定逻辑：实测说话")
    print("=" * 76)
    A = "u_thumb"
    Bp = "u_orig"
    declared = (1440, 2559)

    # C1 缩略档实测更小 → 选原图
    picked, note = B.verify_best_image([A, Bp], declared,
                                       {A: (960, 1706), Bp: (2160, 3838)})
    check("缩略档被识破，选中实测更大的原图", picked == Bp, f"{picked} / {note}")

    # C2 原图被误报成缩略（实测比声明还小）→ 仍要给出最大的那个，而不是崩
    picked2, note2 = B.verify_best_image([A, Bp], declared,
                                         {A: (1200, 2133), Bp: (1400, 2489)})
    check("都比声明小时取实测最大的", picked2 == Bp, f"{picked2} / {note2}")

    # C3 探测全失败 → 回退到首选，不改行为
    picked3, note3 = B.verify_best_image([A, Bp], declared, {A: None, Bp: None})
    check("探测全失败时回退首选", picked3 == A, f"{picked3} / {note3}")
    check("回退时给出说明", bool(note3), note3)

    # C4 没有声明尺寸 → 不做判断，取首选
    picked4, note4 = B.verify_best_image([A, Bp], (0, 0), {A: (1, 1), Bp: (9, 9)})
    check("无声明尺寸时取首选", picked4 == A, f"{picked4} / {note4}")

    # C5 空候选
    picked5, note5 = B.verify_best_image([], declared, {})
    check("空候选返回 None", picked5 is None, f"{picked5} / {note5}")

    print()
    print("=" * 76)
    print("D) 开关与缓存：可关闭、不缓存失败")
    print("=" * 76)
    saved = B._PROBE_IMAGE
    try:
        B._PROBE_IMAGE = False
        got = asyncio.run(B.measure_image_size("https://example.com/x.jpg"))
        check("关闭探测时直接返回 None（不发请求）", got is None, str(got))
        check("关闭原因可读", "关闭" in B._probe_disabled_reason(), B._probe_disabled_reason())
    finally:
        B._PROBE_IMAGE = saved

    print()
    print("=" * 76)
    print("E) 真实网络（可选）：Range 探测真实图片")
    print("=" * 76)
    real = os.environ.get("DOUYIN_TEST_IMG", "").strip()
    if not real:
        print("  跳过（未提供 DOUYIN_TEST_IMG）")
    else:
        got = asyncio.run(B.measure_image_size(real))
        check("真实图片能解析出宽高", bool(got and got[0] > 0 and got[1] > 0), str(got))
        if got:
            print(f"    实测 = {got[0]}×{got[1]}")
        # 再要一次应该命中缓存
        again = asyncio.run(B.measure_image_size(real))
        check("第二次命中缓存（结果一致）", again == got, f"{got} vs {again}")

    print()
    print("=" * 76)
    print("F) 本地 HTTP 端到端：真实走网络，验证「缩略档被识破、选中原图」")
    print("=" * 76)
    if not _module_available("httpx"):
        print("  跳过（无 httpx，无法跑真实请求）")
    else:
        import http.server
        import threading

        thumb = jpeg_header(960, 1706)
        orig = jpeg_header(2160, 3838)

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = thumb if "thumb" in self.path else orig
                rng = self.headers.get("Range")
                if rng:  # 支持 Range，模拟 CDN
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes 0-{len(body)-1}/{len(body)}")
                    self.send_header("Content-Length", str(len(body)))
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"

        # 两个不同档位：thumbresize 是小图，aweme-images 是原图
        fake = {
            "width": 1440, "height": 2559,
            "url_list": [f"{base}/thumb~tplv-resize:1440:2559:q75.jpeg",
                         f"{base}/orig~tplv-dy-aweme-images:q75.jpeg"],
        }
        try:
            m = {
                fake["url_list"][0]: asyncio.run(B.measure_image_size(fake["url_list"][0])),
                fake["url_list"][1]: asyncio.run(B.measure_image_size(fake["url_list"][1])),
            }
            check("小图实测 960×1706", m[fake["url_list"][0]] == (960, 1706), str(m[fake["url_list"][0]]))
            check("原图实测 2160×3838", m[fake["url_list"][1]] == (2160, 3838), str(m[fake["url_list"][1]]))
            picked, note = B.verify_best_image(
                [fake["url_list"][0], fake["url_list"][1]], (1440, 2559), m)
            check("端到端选中原图（缩略档被识破）", "aweme-images" in picked, f"{picked} / {note}")
            print(f"    说明: {note}")
        finally:
            srv.shutdown()

    print()
    print("=" * 76)
    print(f"结果: {_ok} 通过, {_bad} 失败")
    print("=" * 76)
    return 0 if _bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

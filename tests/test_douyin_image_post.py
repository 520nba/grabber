"""抖音「图文作品的占位 video」回归测试。

实测背景（2026-09，作品 7690957388573044089，aweme_type=68 图文）：

    video.width/height      = 2160×3838     ← 其实是封面尺寸
    video.play_addr 宽高    = 720×720       ← 占位方图
    video.duration          = 0             ← 0 秒
    bit_rate / play_addr_h264 / play_addr_265 = 全 None
    该 play_addr 的 URL 请求回来是 content-type: audio/mp4（1.8MB 背景音）

旧实现只判断 ``url_list`` 非空就收下，于是把这 1.8MB 音频当成「视频」返回，
还安上 720×720 的假尺寸 —— 前端拿 kind:"video" 去播 audio/mp4，必然失败，
用户看到的现象就是「抖音视频无法解析」。

本测试锁定：
  1. 图文作品的占位 video 不得产出任何视频候选；
  2. 图文作品仍然要能正常取到 2160×3838 的原图档；
  3. ``_extract_items`` 对「图文+占位video」返回 image item 并附一句人话说明；
  4. 真实视频作品（http/h265 档、duration>0）不得被这些规则误伤；
  5. duration>0 的正方形视频不得被「方图=占位」规则误伤。

不需要第三方依赖（httpx / gmssl 用最小桩顶掉）。

运行：
    python tests/test_douyin_image_post.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG = REPO / "grabber"
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

def _module_available(name: str) -> bool:
    """模块是否真的可用（find_spec 对被换成无 __spec__ 的桩会抛 ValueError）。"""
    mod = sys.modules.get(name)
    if mod is not None:
        return getattr(mod, "__spec__", None) is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# 只在**真的没装**时才打桩：用 _module_available 而不是 "httpx" not in sys.modules，
# 后者在导入 parsers 之前必然成立，会把桩装上、遮住环境里的真库。
if not _module_available("httpx"):
    _stub = types.ModuleType("httpx")

    class _StubClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise RuntimeError("stub httpx 不应被调用")

    _stub.AsyncClient = _StubClient
    _stub.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stub"))
    _stub.__spec__ = None
    sys.modules["httpx"] = _stub

if not _module_available("gmssl"):
    _gm = types.ModuleType("gmssl")
    _gm.sm3 = types.ModuleType("gmssl.sm3")
    _gm.func = types.ModuleType("gmssl.func")
    _gm.sm3.sm3_hash = lambda *a, **k: ""
    _gm.func.bytes_to_list = lambda b: list(b)
    _gm.func.list_to_bytes = lambda l: bytes(l)
    sys.modules["gmssl"] = _gm
    sys.modules["gmssl.sm3"] = _gm.sm3
    sys.modules["gmssl.func"] = _gm.func

from parsers.base import (  # noqa: E402
    _dy_is_audio_stream,
    douyin_image_pick,
    douyin_video_candidates,
)
from parsers.web_api import extract_items  # noqa: E402

# ── 精简 fixture：保留实测结构，去掉一切 Cookie / 签名参数 ────────────────
IMAGE_OBJ = {
    "width": 2160,
    "height": 3838,
    "url_list": [
        "https://p3-pc-sign.douyinpic.com/tos-cn-i-0813c000-ce/OBJKEY~tplv-dy-aweme-images:q75.webp",
        "https://p9-pc-sign.douyinpic.com/tos-cn-i-0813c000-ce/OBJKEY~tplv-dy-aweme-images:q75.webp",
        "https://p3-pc-sign.douyinpic.com/tos-cn-i-0813c000-ce/OBJKEY~tplv-dy-aweme-images:q75.jpeg",
    ],
}

# 图文作品自带的「占位 video」：实际是背景音
PLACEHOLDER_VIDEO = {
    "duration": 0,
    "width": 2160,
    "height": 3838,
    "is_h265": 0,
    "has_watermark": False,
    "play_addr": {
        "width": 720,
        "height": 720,
        "uri": "https://sf11-cdn-tos.douyinstatic.com/obj/tos-cn-ve-2774/AUDIOKEY",
        "url_list": [
            "https://sf11-cdn-tos.douyinstatic.com/obj/tos-cn-ve-2774/AUDIOKEY",
            "https://sf6-cdn-tos.douyinstatic.com/obj/tos-cn-ve-2774/AUDIOKEY",
        ],
    },
    "cover": {"url_list": ["https://p9-pc-sign.douyinpic.com/tos-cn-i-0813c000-ce/OBJKEY~noop.jpeg"]},
    "bit_rate": None,
    "play_addr_h264": None,
    "play_addr_265": None,
}

REAL_VIDEO = {
    "duration": 15000,
    "width": 1080,
    "height": 1920,
    "play_addr": {
        "width": 1080, "height": 1920,
        "url_list": ["https://www.douyin.com/aweme/v1/play/?video_id=v0200default"],
    },
    "play_addr_h264": {
        "width": 720, "height": 1280,
        "url_list": ["https://www.douyin.com/aweme/v1/play/?video_id=v0720h264"],
    },
    "bit_rate": [
        {"bit_rate": 2000, "is_h265": False,
         "play_addr": {"width": 1080, "height": 1920,
                       "url_list": ["https://www.douyin.com/aweme/v1/play/?video_id=v1080h264"]}},
        {"bit_rate": 2500, "is_h265": True,
         "play_addr": {"width": 1080, "height": 1920,
                       "url_list": ["https://www.douyin.com/aweme/v1/play/?video_id=v1080h265"]}},
    ],
}

_passed = _failed = 0


def _check(label: str, ok: bool, extra: str = "") -> None:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed += 1
        print(f"  FAIL  {label}  {extra}")


def main() -> int:
    print("1) 图文作品仍能取到 2160×3838 的 jpeg 原图档")
    best, alt = douyin_image_pick(IMAGE_OBJ)
    _check("选中 ~tplv-dy-aweme-images:q75.jpeg",
           "dy-aweme-images" in best and ".jpeg" in best, best)
    _check("备用档与主档不同源主机",
           alt is not None and alt != best, str(alt))

    print()
    print("2) 图文作品的占位 video 不产出候选")
    _check("候选数 == 0", len(douyin_video_candidates(PLACEHOLDER_VIDEO)) == 0,
           f"得到 {len(douyin_video_candidates(PLACEHOLDER_VIDEO))}")
    _check("douyinstatic 识别为音频",
           _dy_is_audio_stream(PLACEHOLDER_VIDEO["play_addr"]["url_list"][0]))

    print()
    print("3) extract_items 对「图文+占位video」的处理")
    # 注意：fixture 里只有单一变换段，所以不会触发实测探测（走同步快速路径），
    # 测试因此不需要 mock 网络。探测路径由 test_image_probe.py 覆盖。
    items, meta = asyncio.run(extract_items({
        "aweme_detail": {"desc": "🩹", "author": {"nickname": "海77"},
                         "aweme_type": 68, "images": [IMAGE_OBJ],
                         "video": PLACEHOLDER_VIDEO},
    }))
    kinds = [i["kind"] for i in items]
    _check("返回 1 个 image item", kinds == ["image"], str(kinds))
    _check("不返回 video item", "video" not in kinds)
    _check("给出图文说明（no_video_note）", bool(meta.get("no_video_note")))
    _check("说明里点明不是视频", "图文" in (meta.get("no_video_note") or ""))

    print()
    print("4) 真实视频作品不被误伤")
    rc = douyin_video_candidates(REAL_VIDEO)
    _check("候选数 >= 3", len(rc) >= 3, f"得到 {len(rc)}")
    _check("最高码率(h265)排首位",
           bool(rc) and rc[0]["bit_rate"] == 2500 and rc[0]["is_h265"] is True)
    items2, meta2 = asyncio.run(extract_items({
        "aweme_detail": {"desc": "v", "author": {}, "aweme_type": 0,
                         "images": None, "video": REAL_VIDEO},
    }))
    _check("仍返回 video item", [i["kind"] for i in items2] == ["video"])
    _check("真实视频不带图文说明", not meta2.get("no_video_note"))

    print()
    print("5) duration>0 的正方形视频不被「方图=占位」规则误伤")
    square = {"duration": 8000,
              "play_addr": {"width": 1080, "height": 1080,
                            "url_list": ["https://www.douyin.com/aweme/v1/play/?video_id=square"]}}
    _check("候选数 == 1", len(douyin_video_candidates(square)) == 1)

    print()
    print("=" * 76)
    print(f"结果: {_passed} 通过, {_failed} 失败")
    print("=" * 76)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

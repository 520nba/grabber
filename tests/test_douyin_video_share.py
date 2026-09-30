"""验证抖音「视频无法解析」的两个根因修复。

背景（这条 bug 排查代价很高，测试把两个坑都钉住）：

1. **嵌套结构没被遍历**
   视频分享页把作品数据放在
       loaderData["video_(id)/page"]["videoInfoRes"]["item_list"]
   `_from_page` 的遍历只在**顶层**判断 `"item_list" in node`，而这一层是
   `videoInfoRes`（它的值是 `item_list`，键名不是），于是整棵被跳过。
   图文页的 item_list 在别的层级，所以表现为「图文一直正常、视频一直失败」——
   极易误判成风控/签名问题。

2. **分享页唯一的地址被去水印过滤清空**
   分享页只给带水印的 `/aweme/v1/playwm/`，而 `douyin_video_candidates`
   会跳过所有含 `playwm` 的地址（那是为 detail 接口设计的），结果分享页
   拿到作品却产出 0 个候选。实测把 `playwm` → `play` 后返回**真实 MP4 字节**
   （HTTP 200、content-type video/mp4、ftyp 头），且**不需要 a_bogus 签名**。

全程无网络：用真实抓取到的结构做 fixture。
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "grabber"))

_ok = _bad = 0


def check(label, cond, extra=""):
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"  PASS  {label}")
    else:
        _bad += 1
        print(f"  FAIL  {label}  {extra}")


def _module_available(name: str) -> bool:
    """模块是否真的可用（不抛异常）。

    不能直接用 find_spec：若 sys.modules[name] 已被换成没有 __spec__ 的桩，
    find_spec 会抛 `ValueError: <name>.__spec__ is None`。
    """
    mod = sys.modules.get(name)
    if mod is not None:
        return getattr(mod, "__spec__", None) is not None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


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
    _g = types.ModuleType("gmssl")
    _g.sm3 = types.ModuleType("gmssl.sm3")
    _g.func = types.ModuleType("gmssl.func")
    _g.sm3.sm3_hash = lambda *a, **k: ""
    _g.func.bytes_to_list = lambda b: list(b)
    _g.func.list_to_bytes = lambda l: bytes(l)
    sys.modules["gmssl"] = _g
    sys.modules["gmssl.sm3"] = _g.sm3
    sys.modules["gmssl.func"] = _g.func

from parsers.base import douyin_video_candidates  # noqa: E402
from parsers.douyin import _from_page, _share_play_candidates  # noqa: E402

# ── 真实抓取到的结构（变量名/层级保持原样） ───────────────────────────
PLAYWM = ("https://aweme.snssdk.com/aweme/v1/playwm/"
          "?video_id=v1e00fgi0000dau5q87og65onet1u91g&ratio=720p&line=0")

VIDEO_OBJ = {
    "play_addr": {"uri": "v1e00fgi0000dau5q87og65onet1u91g", "url_list": [PLAYWM]},
    "cover": {"url_list": ["https://p3.douyinpic.com/cover.jpeg"]},
    "width": 720, "height": 1280, "bit_rate": [], "duration": 9434,
}

# 视频分享页：数据嵌在 videoInfoRes 里（这是关键结构）
VIDEO_PAYLOAD = {
    "loaderData": {
        "video_layout": None,
        "video_(id)/page": {
            "host": "m.douyin.com",
            "renderInSSR": 1,
            "videoInfoRes": {
                "status_code": 0,
                "item_list": [{
                    "aweme_id": "7691124687846068838",
                    "desc": "被自己的可爱贴纸萌到了",
                    "author": {"nickname": "某人"},
                    "aweme_type": 0,
                    "video": VIDEO_OBJ,
                }],
            },
        },
    },
    "errors": None,
}

# 图文作品：video 是占位（duration=0、方图、音频流）
IMAGE_PLACEHOLDER_VIDEO = {
    "duration": 0, "width": 720, "height": 720, "bit_rate": [],
    "play_addr": {"url_list": [
        "https://sf11-cdn-tos.douyinstatic.com/obj/audio/music/track.mp4"]},
}

# 含真视频的多档 bit_rate（detail 接口形态），用于确认新逻辑不抢它的活
DETAIL_VIDEO = {
    "duration": 11934, "width": 1440, "height": 2560,
    "play_addr": {"url_list": ["https://v3-web.douyinvod.com/aaa/video.mp4"]},
    "bit_rate": [
        {"bit_rate": 1835101, "is_h265": 1,
         "play_addr": {"width": 1440, "height": 2560,
                       "url_list": ["https://v3-web.douyinvod.com/hi/video.mp4"]}},
        {"bit_rate": 2053040, "is_h265": 0,
         "play_addr": {"width": 1080, "height": 1920,
                       "url_list": ["https://v3-web.douyinvod.com/lo/video.mp4"]}},
    ],
}


def main() -> int:
    print("=" * 76)
    print("A) 根因1：嵌套的 videoInfoRes.item_list 必须能被遍历到")
    print("=" * 76)
    items, meta = _from_page(VIDEO_PAYLOAD)
    check("视频分享页能解析出 item", len(items) == 1, f"items={len(items)}")
    check("类型是 video", items and items[0]["kind"] == "video", str(items[:1]))
    check("标题取到", meta.get("title") == "被自己的可爱贴纸萌到了", meta.get("title"))
    check("作者取到", meta.get("author") == "某人", meta.get("author"))
    if items:
        check("时长正确（9434ms -> 9.434s）", abs((items[0]["duration"] or 0) - 9.434) < 0.01,
              str(items[0]["duration"]))
    # 旧实现的行为：把 videoInfoRes 那层当成不相关节点跳过
    check("模拟旧逻辑（顶层查找）确实找不到",
          not any(isinstance(v, dict) and "item_list" in v
                  for v in VIDEO_PAYLOAD["loaderData"].values()),
          "若这里为 True，说明 fixture 不再能代表当初的 bug")

    print()
    print("=" * 76)
    print("B) 根因2：分享页 playwm 必须转成可用的直链")
    print("=" * 76)
    check("douyin_video_candidates 会跳过 playwm（设计如此）",
          douyin_video_candidates(VIDEO_OBJ) == [], str(douyin_video_candidates(VIDEO_OBJ)))
    c = _share_play_candidates(VIDEO_OBJ)
    check("分享候选能拿到 1 条", len(c) == 1, f"n={len(c)}")
    if c:
        u = c[0]["url"]
        check("已去掉 playwm（去水印）", "playwm" not in u, u)
        check("走的是 play 端点", "/aweme/v1/play/" in u, u)
        check("ratio 提到 1080p", "ratio=1080p" in u, u)
        check("保留 video_id", "video_id=v1e00fgi0000dau5q87og65onet1u91g" in u, u)
        check("标记 from_share=True", c[0]["from_share"] is True, str(c[0]))
        check("码率标 0（分享页无分档，不谎报）", c[0]["bit_rate"] == 0, str(c[0]))
    items2, _ = _from_page(VIDEO_PAYLOAD)
    if items2:
        check("_from_page 走后一个候选并标 from_share",
              items2[0].get("from_share") is True and "playwm" not in items2[0]["url"],
              str(items2[0].get("url")))

    print()
    print("=" * 76)
    print("C) 不能倒退：图文占位视频 / 多档 detail 的行为")
    print("=" * 76)
    check("图文占位（音频流）不产出分享候选",
          _share_play_candidates(IMAGE_PLACEHOLDER_VIDEO) == [],
          str(_share_play_candidates(IMAGE_PLACEHOLDER_VIDEO)))
    detail_cands = douyin_video_candidates(DETAIL_VIDEO)
    check("detail 多档仍能拿到候选", len(detail_cands) >= 2, f"n={len(detail_cands)}")
    if detail_cands:
        check("detail 首选仍是面积最大的 1440×2560",
              detail_cands[0]["width"] == 1440 and detail_cands[0]["height"] == 2560,
              str(detail_cands[0]))
        check("detail 不标 from_share", not detail_cands[0].get("from_share"), str(detail_cands[0]))
    # 有真视频时不应退到分享候选
    payload_detail = {"loaderData": {"video_(id)/page": {"videoInfoRes": {
        "item_list": [{"aweme_id": "1", "desc": "d", "author": {},
                       "video": DETAIL_VIDEO}]}}}}
    items3, _ = _from_page(payload_detail)
    check("有 bit_rate 时不走分享兜底",
          items3 and not items3[0].get("from_share"), str(items3[:1]))

    print()
    print("=" * 76)
    print("D) 分享候选的通用性（含 download_addr / h264 变体）")
    print("=" * 76)
    multi = {
        "duration": 5000, "width": 1080, "height": 1920,
        "play_addr": {"url_list": [PLAYWM, "https://aweme.snssdk.com/aweme/v1/play/?video_id=XYZ"]},
        "download_addr": {"url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=DL"]},
        "play_addr_h264": {"url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=H264"]},
    }
    cm = _share_play_candidates(multi)
    urls = [x["url"] for x in cm]
    check("多来源都收拢（>=3 条）", len(cm) >= 3, str(len(cm)))
    check("全部无 playwm", all("playwm" not in u for u in urls), str(urls))
    check("全部 ratio=1080p", all("ratio=1080p" in u for u in urls), str(urls))
    check("去重（同一 video_id 只留一条）",
          len(urls) == len(set(urls)), str(urls))
    check("无音频串入", all("music" not in u for u in urls), str(urls))

    print()
    print("=" * 76)
    print(f"结果: {_ok} 通过, {_bad} 失败")
    print("=" * 76)
    return 0 if _bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

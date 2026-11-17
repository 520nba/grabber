"""文件名消毒的回归测试。

背景：ComfyUI 的 /view 对**任何**含 ".." 的文件名一律 400（把子串当路径穿越），
所以下载器一旦把「标题里的 ...」带进文件名，文件虽然落盘成功，但进了素材库 /
ComfyUI input 之后所有预览节点都打不开（实测 61 个文件全部预览空白）。
本测试盯住两处产地：
  * 服务端 server.safe_file_stem / safe_download_name（落盘前缀 + 手机端下载名）；
  * 前端 static/index.html 的 safeName()（"下载到「文件」App"的名字与 NAS 前缀默认值）。

运行（需要 httpx；不联网，HTTP 全部走 MockTransport）：
    python tests/test_filename_safety.py
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from urllib.parse import quote, unquote

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "grabber"))

import httpx  # noqa: E402
import server  # noqa: E402

_ok = _bad = 0


def check(label, cond, extra=""):
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"  PASS  {label}")
    else:
        _bad += 1
        print(f"  FAIL  {label}  {extra}")


with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    try:
        _loop = asyncio.get_event_loop_policy().get_event_loop()
        if _loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)


def run(coro):
    return _loop.run_until_complete(coro)


JPEG = b"\xff\xd8\xff" + b"x" * 400
FAKE_URL = "https://p3-pc-sign.douyinpic.com/tos-cn-i/OBJKEY~tplv-dy-aweme-images:q75.jpeg"


def mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=JPEG,
            headers={"content-type": "image/jpeg", "content-length": str(len(JPEG))},
        )
    return httpx.MockTransport(handler)


async def _install(transport):
    # httpx 0.28 在 trust_env 下会把环境里的代理自动挂进 _mounts，而 _mounts 的优先级
    # 高于 _transport —— 不清掉的话 MockTransport 根本不会被调用（请求会真发出去）。
    for client in (server.get_http_client(), server.get_stream_client()):
        client._mounts.clear()
        client._transport = transport


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class FakeVidRequest:
    headers: dict = {}


BAD_FRAGMENTS = ".."
FORBIDDEN_PREFIX = ". "


def is_safe_name(name: str) -> bool:
    """落盘名必须满足 ComfyUI /view 与 Windows 两边的限制。"""
    if not name:
        return False
    if BAD_FRAGMENTS in name:
        return False
    if name != name.strip(" "):
        return False
    if name[0] in " ._":
        return False
    if name[-1] in " .":
        return False
    if re.search(r'[\\/:*?"<>|\x00-\x1f]', name):
        return False
    return True


def main() -> int:
    print("=" * 76)
    print("1) safe_file_stem：带 ... 的标题必须折叠掉（ComfyUI /view 的 400 根因）")
    print("=" * 76)
    cases = {
        "天呐这个龙凤褂一上身直接给到一个...": "天呐这个龙凤褂一上身直接给到一个",
        "a...b": "a_b",
        "a....b": "a_b",
        "...开头": "开头",
        "结尾...": "结尾",
        "a..b..c": "a_b_c",
        "标题.带.单点": "标题.带.单点",
        "  .前后空格.  ": "前后空格",
    }
    for raw, expected in cases.items():
        got = server.safe_file_stem(raw)
        check(f"{raw!r} → {got!r}", got == expected and is_safe_name(got), f"expected {expected!r}")
    check("空标题得到空串（调用方兜底序号）", server.safe_file_stem("") == "")
    check("None 得到空串", server.safe_file_stem(None) == "")
    check("只有点/空格/下划线 → 空串", server.safe_file_stem(" ... _ ") == "")
    check("Windows 非法字符被替换",
          server.safe_file_stem('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j",
          server.safe_file_stem('a/b\\c:d*e?f"g<h>i|j'))
    check("控制字符被替换", server.safe_file_stem("a\nb\tc") == "a_b_c", server.safe_file_stem("a\nb\tc"))
    check("保留名 CON 前缀加下划线", server.safe_file_stem("CON") == "_CON", server.safe_file_stem("CON"))
    check("保留名 nUl 大小写无关", server.safe_file_stem("nUl") == "_nUl", server.safe_file_stem("nUl"))
    long_stem = server.safe_file_stem("很长" * 100, 40)
    check("截断到 40 个字符", len(long_stem) == 40, str(len(long_stem)))
    check("截断后依然安全", is_safe_name(long_stem), long_stem)
    tricky = server.safe_file_stem("a" + "." * 39 + "b", 40)
    check("截断不会留下 ..", is_safe_name(tricky), tricky)
    check("emoji 标题不报错且安全",
          is_safe_name(server.safe_file_stem("🎉🎉...标题🎉")), server.safe_file_stem("🎉🎉...标题🎉"))

    print()
    print("=" * 76)
    print("2) safe_download_name：手机端「下载到文件 App」的名字")
    print("=" * 76)
    got = server.safe_download_name("天呐这个龙凤褂一上身直接给到一个...太好看了.mp4")
    check("... 被折叠", ".." not in got, got)
    check("扩展名保留", got.endswith(".mp4"), got)
    got = server.safe_download_name("作品.MP4")
    check("扩展名小写化", got == "作品.mp4", got)
    got = server.safe_download_name("没有扩展名")
    check("无扩展名时不硬塞", got == "没有扩展名", got)
    check("空名字回落到默认", server.safe_download_name("") == "media.mp4")
    check("只有点也回落", server.safe_download_name("...") == "media.mp4", server.safe_download_name("..."))
    got = server.safe_download_name('标题"带引号\n换行...结尾.mp4')
    check("引号/换行被清理", is_safe_name(got), got)
    check("扩展名里的怪字符被丢掉", server.safe_download_name("a...b.m!p@4") == "a_b.mp4",
          server.safe_download_name("a...b.m!p@4"))

    print()
    print("=" * 76)
    print("3) /api/download：落进 NAS 的文件名不含 ..（真实走一遍落盘）")
    print("=" * 76)
    run(_install(mock_transport()))
    tmp = Path(tempfile.mkdtemp(prefix="grabber_names_"))
    original_dir = server.DOWNLOAD_DIR
    server.DOWNLOAD_DIR = str(tmp)
    try:
        payload = {"urls": [FAKE_URL, FAKE_URL], "kinds": ["image", "image"],
                   "subdir": "天呐这个龙凤褂一上身直接给到一个..."}
        result = run(server.api_download(FakeRequest(payload)))
        files = [row.get("file") for row in result.get("results") or []]
        check("两个都 saved", result.get("saved") == 2, str(result)[:160])
        for name in files:
            check(f"落盘名安全：{name!r}", is_safe_name(name), name)
            check(f"文件存在：{name!r}", (tmp / name).exists(), name)
        check("标题里的 ... 没进文件名", all(".." not in (name or "") for name in files), str(files))

        # 用户在前端手填的前缀也可能带 ...（subdir 输入框是自由文本）
        result2 = run(server.api_download(
            FakeRequest({"urls": [FAKE_URL], "kinds": ["image"], "subdir": "手填..前缀..."})))
        name2 = (result2.get("results") or [{}])[0].get("file")
        check("手填前缀也被消毒", is_safe_name(name2), name2)

        # 空前缀：只留序号，仍然安全
        result3 = run(server.api_download(
            FakeRequest({"urls": [FAKE_URL], "kinds": ["image"], "subdir": "..."})))
        name3 = (result3.get("results") or [{}])[0].get("file")
        check("纯点前缀退化为序号名", name3 == "01.jpeg", name3)
    finally:
        server.DOWNLOAD_DIR = original_dir

    print()
    print("=" * 76)
    print("4) /api/video?dl=1：Content-Disposition 里的名字不含 ..")
    print("=" * 76)
    run(_install(mock_transport()))
    response = run(server.api_video(
        u=FAKE_URL, request=FakeVidRequest(),
        name="天呐这个龙凤褂一上身直接给到一个...太好看了.mp4", dl=1))
    header = response.headers.get("content-disposition", "")
    check("响应带 Content-Disposition", "attachment" in header, header)
    match = re.search(r"filename\*=UTF-8''([^;]+)", header)
    decoded = unquote(match.group(1)) if match else ""
    check("文件名头部可解析", bool(match), header[:120])
    check("下载名不含 ..", ".." not in decoded, decoded)
    check("下载名以 .mp4 结尾", decoded.endswith(".mp4"), decoded)
    check("HTTP 头仍是 latin-1 可编码（中文必须百分号编码）",
          header.encode("latin-1", "strict") is not None)

    print()
    print("=" * 76)
    print("5) 前端 safeName()：从 index.html 抽出来，用 node 真跑")
    print("=" * 76)
    html = (REPO / "grabber" / "static" / "index.html").read_text(encoding="utf-8")
    fn = re.search(r"function safeName\(s\)\{.*?\n\}", html, re.S)
    check("能在 index.html 找到 safeName()", fn is not None)
    if fn is not None:
        js = fn.group(0) + """
const cases = ["天呐这个龙凤褂一上身直接给到一个...",
  "a...b", "...开头", "结尾...", "标题.带.单点", "普通标题",
  "🎉🎉...标题🎉", "a/b\\\\c:d"];
console.log(JSON.stringify(cases.map((s) => [s, safeName(s)])));
"""
        proc = subprocess.run(
            ["node", "-e", js], capture_output=True, text=True, encoding="utf-8", timeout=60)
        check("node 执行 safeName", proc.returncode == 0, proc.stderr[-300:])
        if proc.returncode == 0:
            pairs = json.loads(proc.stdout.strip().splitlines()[-1])
            for raw, got in pairs:
                check(f"前端 {raw!r} → {got!r}", ".." not in got and got, got)
            longest = max(len(g) for _r, g in pairs); check("前端截断到 24 个码点", longest <= 24, str(longest))

            check("前端保留普通标题", dict(pairs).get("普通标题") == "普通标题", dict(pairs).get("普通标题"))

    print()
    print("=" * 76)
    print(f"SUMMARY: pass={_ok} fail={_bad} result={'PASS' if _bad == 0 else 'FAIL'}")
    return 0 if _bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

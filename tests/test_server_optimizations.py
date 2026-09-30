"""验证本轮四项优化：下载不覆盖、共享客户端、图片代理流式+上限、写入不阻塞。

用 httpx MockTransport 注入假上游，全程不发真实网络请求。

运行（需要 httpx）：
    python tests/test_server_optimizations.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import warnings
from pathlib import Path

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


# ── event loop：固定一个，供共享 httpx 客户端复用 ─────────────────────────
# 不能每次 asyncio.run()：那会每次新建 loop，而共享客户端绑定在创建它的 loop 上，
# 跨 loop 用会报 "attached to a different loop"。生产代码已加 loop 归属检测可自愈，
# 但测试里固定一个 loop 更能暴露真问题。
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


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 500


def mock_transport(*, size=None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = PNG if size is None else b"y" * size
        return httpx.Response(
            200, content=body,
            headers={"content-type": "image/png", "content-length": str(len(body))},
        )
    return httpx.MockTransport(handler)


async def _install_async(transport):
    """在**事件循环内**装 mock transport。

    必须在循环内做：get_*_client 会检查客户端是否属于当前 loop；循环外取到时
    loop 为 None，进循环后会被判成「换了 loop」而重建，transport 就白装了。
    """
    server.get_http_client()._transport = transport
    server.get_stream_client()._transport = transport


def install(transport):
    run(_install_async(transport))


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class FakeVidRequest:
    headers: dict = {}


FAKE_URL = "https://p3-pc-sign.douyinpic.com/tos-cn-i/OBJKEY~tplv-dy-aweme-images:q75.jpeg"


def main() -> int:
    print("=" * 76)
    print("1) _unique_dest：同名不覆盖")
    print("=" * 76)
    tmp = Path(tempfile.mkdtemp(prefix="grabber_dl_"))
    (tmp / "作品01.jpg").write_bytes(b"OLD")
    name1, path1 = server._unique_dest(str(tmp), "作品01.jpg")
    check("已存在时改名", name1 == "作品01a.jpg", name1)
    check("返回路径与文件名一致", Path(path1).name == name1)
    (tmp / name1).write_bytes(b"OLD2")
    name2, _ = server._unique_dest(str(tmp), "作品01.jpg")
    check("第二次冲突递增到 b", name2 == "作品01b.jpg", name2)
    nah, _ = server._unique_dest(str(tmp), "全新02.jpg")
    check("无冲突时保持原名", nah == "全新02.jpg", nah)

    print()
    print("=" * 76)
    print("2) 共享客户端：同一实例 + 关闭后可重建")
    print("=" * 76)
    install(mock_transport())
    c1 = server.get_http_client()
    check("get_http_client 返回同一实例", c1 is server.get_http_client())
    s1 = server.get_stream_client()
    check("get_stream_client 返回同一实例", s1 is server.get_stream_client())
    check("两个客户端相互独立", c1 is not s1)
    run(server.close_http_client())
    check("关闭后 is_closed", c1.is_closed and s1.is_closed)
    c3 = server.get_http_client()
    check("关闭后再取会重建新实例", c3 is not c1 and not c3.is_closed)
    install(mock_transport())

    print()
    print("=" * 76)
    print("3) /api/download：真正落盘 + 重下不覆盖")
    print("=" * 76)
    server.DOWNLOAD_DIR = str(tmp)
    payload = {"urls": [FAKE_URL], "kinds": ["image"], "subdir": "作品"}
    r1 = run(server.api_download(FakeRequest(payload)))
    check("第一次下载 saved==1", r1.get("saved") == 1, str(r1)[:140])
    check("结果里有 file 字段", "file" in (r1.get("results") or [{}])[0], str(r1.get("results"))[:120])
    f1 = r1["results"][0]["file"]
    # 扩展名由 _ext_for 从 URL 推导：该 URL 结尾是 :q75.jpeg，所以是 .jpeg 不是 .jpg
    check("落盘文件名正确", f1 == "作品01.jpeg", f1)
    check("文件确实写出且非空", (tmp / f1).exists() and (tmp / f1).stat().st_size > 0)
    r2 = run(server.api_download(FakeRequest(payload)))
    f2 = r2["results"][0]["file"]
    check("重下得到不同文件名（不覆盖）", f2 != f1, f"{f1} vs {f2}")
    check("两个文件同时存在", (tmp / f1).exists() and (tmp / f2).exists())

    print()
    print("=" * 76)
    print("4) /api/img：正常返回 + 超限被拒")
    print("=" * 76)
    r = run(server.api_img(FAKE_URL))
    check("正常图片 200", getattr(r, "status_code", None) == 200, str(type(r)))
    check("响应体等于上游内容", getattr(r, "body", b"") == PNG)

    install(mock_transport(size=server.MAX_IMAGE_BYTES + 5000))
    r_big = run(server.api_img(FAKE_URL))
    check("带 Content-Length 的超大图被拒（502）",
          getattr(r_big, "status_code", None) == 502,
          f"得到 {getattr(r_big, 'status_code', type(r_big))}")
    txt = getattr(r_big, "body", b"").decode("utf-8", "replace")
    check("错误信息点明超限", ("上限" in txt) or ("过大" in txt), txt[:120])

    # 无 Content-Length：靠累计长度拦住
    install(mock_transport(size=server.MAX_IMAGE_BYTES + 5000))
    def no_len(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"z" * (server.MAX_IMAGE_BYTES + 5000),
                              headers={"content-type": "image/png"})
    httpx_client = server.get_http_client()
    httpx_client._transport = httpx.MockTransport(no_len)
    r_big2 = run(server.api_img(FAKE_URL))
    check("无 Content-Length 时靠累计长度拦住",
          getattr(r_big2, "status_code", None) == 502,
          f"得到 {getattr(r_big2, 'status_code', type(r_big2))}")

    print()
    print("=" * 76)
    print("5) /api/video：共享流客户端不被响应结束关掉")
    print("=" * 76)
    install(mock_transport(size=1024))
    resp = run(server.api_video(FAKE_URL, FakeVidRequest()))
    check("返回 StreamingResponse", hasattr(resp, "body_iterator"), str(type(resp)))

    async def drain():
        chunks = []
        async for ch in resp.body_iterator:
            chunks.append(ch)
        if resp.background is not None:
            await resp.background()      # 触发 background=BackgroundTask(r.aclose)
        return b"".join(chunks)

    data = run(drain())
    check("流内容完整", data == b"y" * 1024, f"len={len(data)}")
    check("共享 stream client 仍可用（未被关掉）", not server.get_stream_client().is_closed)

    run(server.close_http_client())
    print()
    print("=" * 76)
    print(f"结果: {_ok} 通过, {_bad} 失败")
    print("=" * 76)
    return 0 if _bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

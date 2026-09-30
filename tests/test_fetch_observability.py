"""验证出网可观测性（#4）与按主机节流（#6）。

#4 降级链原来全静默：线上只看到最终 403，不知道中间挂在哪一步。
   现在 fetch 返回 `via`（最终通道）与 `degrades`（失败过的通道及原因），
   并在发生降级时记进环形缓冲 / 当前请求的诊断。
#6 节流原来在 douyin.py 里，是模块级单变量：一个请求挡住所有平台，
   且只覆盖两条路径（短链展开与小红书完全没节流）。现在下沉到 fetch，
   按主机名计时并排队。

全程 mock 掉 curl_cffi / curl / httpx，不发真实网络请求（除节流计时）。
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "grabber"))

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


loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)


def run(coro):
    return loop.run_until_complete(coro)


def fake_resp(url="https://www.douyin.com/x", text="ok"):
    return B.HttpResponse(200, text, url, {})


def install(creq_mode="ok", curl_mode="ok", httpx_mode="ok"):
    """装三条通道的替身。creq_mode/curl_mode/httpx_mode ∈ {"ok","fail","absent"}"""
    import httpx

    # --- curl_cffi ---
    if creq_mode == "absent":
        sys.modules.pop("curl_cffi", None)
    else:
        # 指纹解析器（_resolve_impersonate）会探测 curl_cffi，并靠捕获
        # curl_cffi.requests.exceptions.ImpersonateError 判定「名字不被支持」。
        # 替身必须提供这个子模块，否则解析器会认为 curl_cffi 不可用、
        # 直接降级到系统 curl，本套件就测不到 curl_cffi 分支了。
        class _ImpersonateError(Exception):
            pass

        mod = types.ModuleType("curl_cffi")
        sub = types.ModuleType("curl_cffi.requests")
        exc = types.ModuleType("curl_cffi.requests.exceptions")
        exc.ImpersonateError = _ImpersonateError

        def get(url, impersonate=None, timeout=None, proxy=None, **kw):
            # 指纹探测会打一个必然连不上的本地代理；这里让所有指纹名都「可用」，
            # 但探测请求本身失败（模拟连不上靶子）。
            if "127.0.0.1:1" in str(url) or proxy:
                raise ConnectionError("探测靶子不可达（指纹名有效）")
            if creq_mode == "fail":
                raise RuntimeError("模拟 curl_cffi 失败")
            return types.SimpleNamespace(
                status_code=200, text="via-curl_cffi", url=url,
                headers={}, cookies=types.SimpleNamespace(get_dict=lambda: {}),
            )

        sub.get = get
        sub.exceptions = exc
        mod.requests = sub
        sys.modules["curl_cffi"] = mod
        sys.modules["curl_cffi.requests"] = sub
        sys.modules["curl_cffi.requests.exceptions"] = exc
        # 清掉指纹解析缓存，避免上一段的状态影响本段
        B._IMPERSONATE_CACHE.clear()
        B._IMPERSONATE_TRIED.clear()

    # --- 系统 curl ---
    def fake_curl(url, hdrs, degrades=None):
        if curl_mode == "absent":
            return None
        if curl_mode == "fail":
            if degrades is not None:
                degrades.append("curl: 模拟失败（退出码 28）")
            return None
        return B.HttpResponse(200, "via-curl", url, {})

    B._curl_fallback = fake_curl

    # --- httpx 兜底 ---
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            if httpx_mode == "fail":
                raise RuntimeError("模拟 httpx 失败")
            return types.SimpleNamespace(
                status_code=200, text="via-httpx", url=url,
                headers={}, cookies={},
            )

    B.httpx = types.SimpleNamespace(AsyncClient=FakeClient)
    B._RECENT_FETCHES.clear()


URL = "https://www.douyin.com/aweme/v1/x"


def main() -> int:
    print("=" * 76)
    print("1) #4 全通道正常：via 标明通道，degrades 为空")
    print("=" * 76)
    install()
    r = run(B.fetch(URL, throttle=False))
    # via 现在带上实际选中的指纹，形如 curl_cffi(chrome) —— 这样 curl_cffi 改名时
    # 从诊断里就能看出用的是哪个（见 test_impersonate.py）
    check("via 标明走了 curl_cffi", r.via.startswith("curl_cffi"), r.via)
    check("via 带具体指纹", "(" in r.via and r.via.endswith(")"), r.via)
    check("degrades 为空", r.degrades == [], str(r.degrades))
    check("正常路径不入环形缓冲", len(B._RECENT_FETCHES) == 0, str(len(B._RECENT_FETCHES)))

    print()
    print("=" * 76)
    print("2) #4 curl_cffi 失败 → 降级 curl，且记录原因")
    print("=" * 76)
    install(creq_mode="fail")
    r = run(B.fetch(URL, throttle=False))
    check("via == curl（已降级）", r.via == "curl", r.via)
    check("degrades 记录了 curl_cffi 的失败", any("curl_cffi" in d for d in r.degrades), str(r.degrades))
    check("降级进了环形缓冲", len(B._RECENT_FETCHES) == 1, str(len(B._RECENT_FETCHES)))
    if B._RECENT_FETCHES:
        rec = B._RECENT_FETCHES[0]
        check("缓冲记录含 host/via/status", rec["host"] == "www.douyin.com" and rec["via"] == "curl",
              str(rec))

    print()
    print("=" * 76)
    print("3) #4 两条都失败 → 降级 httpx，degrades 有两条原因")
    print("=" * 76)
    install(creq_mode="fail", curl_mode="fail")
    r = run(B.fetch(URL, throttle=False))
    check("via == httpx", r.via == "httpx", r.via)
    check("degrades 同时含 curl_cffi 与 curl",
          any("curl_cffi" in d for d in r.degrades) and any(d.startswith("curl:") for d in r.degrades),
          str(r.degrades))

    print()
    print("=" * 76)
    print("4) #4 curl 不存在时也要说明原因")
    print("=" * 76)
    install(creq_mode="fail", curl_mode="absent")
    saved_bin = B._CURL_BIN
    B._CURL_BIN = ""
    try:
        r = run(B.fetch(URL, throttle=False))
    finally:
        B._CURL_BIN = saved_bin
    check("via == httpx（无 curl 可执行文件）", r.via == "httpx", r.via)
    check("degrades 说明找不到 curl",
          any("找不到 curl" in d for d in r.degrades), str(r.degrades))

    print()
    print("=" * 76)
    print("5) #4 每请求诊断（ContextVar）：只有降级时才产出")
    print("=" * 76)
    install()
    import parsers  # noqa: E402

    token = parsers.DIAG.set([])
    run(B.fetch(URL, throttle=False))
    check("正常请求不产生诊断", parsers.DIAG.get() == [], str(parsers.DIAG.get()))
    parsers.DIAG.reset(token)

    install(creq_mode="fail")
    token = parsers.DIAG.set([])
    run(B.fetch(URL, throttle=False))
    diag = parsers.DIAG.get()
    parsers.DIAG.reset(token)
    check("降级请求产生 1 条诊断", len(diag) == 1, str(diag))
    check("诊断含 via 与 degrades", diag and diag[0]["via"] == "curl" and diag[0]["degrades"], str(diag))

    print()
    print("=" * 76)
    print("6) #4 fetch_diagnostics 摘要可读且不含敏感信息")
    print("=" * 76)
    d = B.fetch_diagnostics()
    for k in ("curl_cffi_available", "curl_binary", "proxy", "timeout", "host_gaps",
              "default_gap", "recent_degrades"):
        check(f"含字段 {k}", k in d, str(list(d.keys())))
    blob = str(d).lower()
    check("不含 cookie / authorization 字样",
          "cookie" not in blob and "authorization" not in blob, blob[:120])

    print()
    print("=" * 76)
    print("7) #6 同一主机节流生效（间隔 ≥ gap）")
    print("=" * 76)
    B._last_host_at.clear()
    B._HOST_GAP["www.douyin.com"] = 0.6      # 测试用短间隔
    B._DEFAULT_GAP = 0.25
    install()

    async def two_same_host():
        t0 = time.monotonic()
        await B.fetch("https://www.douyin.com/a", throttle=True)
        t1 = time.monotonic()
        await B.fetch("https://www.douyin.com/b", throttle=True)
        t2 = time.monotonic()
        return (t1 - t0), (t2 - t1)

    first, second = run(two_same_host())
    check("第 1 次不等待（< 0.3s）", first < 0.3, f"{first:.3f}s")
    check("第 2 次等待 ≥ gap（0.6s）", second >= 0.55, f"{second:.3f}s")

    print()
    print("=" * 76)
    print("8) #6 不同主机互不阻塞（旧实现会互相挡住）")
    print("=" * 76)
    B._last_host_at.clear()

    async def two_hosts():
        t0 = time.monotonic()
        await B.fetch("https://www.douyin.com/a", throttle=True)      # gap 0.6
        t1 = time.monotonic()
        await B.fetch("https://sns-webpic-qc.xhscdn.com/b", throttle=True)  # 默认 0.25
        t2 = time.monotonic()
        return (t1 - t0), (t2 - t1)

    a, b = run(two_hosts())
    check("第 2 个不同主机不受第 1 个的 gap 约束（< 0.4s）", b < 0.4, f"{b:.3f}s")

    print()
    print("=" * 76)
    print("9) #6 并发同主机必须排队（不能同时冲出去）")
    print("=" * 76)
    B._last_host_at.clear()

    async def concurrent():
        t0 = time.monotonic()
        await asyncio.gather(*[
            B.fetch("https://www.douyin.com/c", throttle=True) for _ in range(3)
        ])
        return time.monotonic() - t0

    total = run(concurrent())
    # 3 次、间隔 0.6s：首个不等待，之后两次各等 ≈0.6s（含抖动上限 0.4）
    check("3 个并发被串行摊开（≥ 1.1s）", total >= 1.1, f"{total:.3f}s")

    print()
    print("=" * 76)
    print("10) #6 throttle=False 可完全跳过（供内部探针使用）")
    print("=" * 76)
    B._last_host_at.clear()

    async def skip():
        t0 = time.monotonic()
        await B.fetch("https://www.douyin.com/d", throttle=False)
        await B.fetch("https://www.douyin.com/e", throttle=False)
        return time.monotonic() - t0

    quick = run(skip())
    check("跳过节流后几乎不等待（< 0.3s）", quick < 0.3, f"{quick:.3f}s")

    print()
    print("=" * 76)
    print(f"结果: {_ok} 通过, {_bad} 失败")
    print("=" * 76)
    return 0 if _bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

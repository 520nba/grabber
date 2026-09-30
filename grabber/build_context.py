"""打包「构建上下文」tar.gz，供 Portainer 的 Build a new image 直接上传。

为什么需要它：Portainer 的 Stack Web editor 没有构建上下文，必须先把镜像单独构建出来，
而构建需要一个压缩包当上下文。之前是临时拼 tar 命令，结果漏掉了 `parsers/__init__.py`
（被 `.dockerignore` 里的 `_*` 排掉），容器启动才报：

    ImportError: cannot import name 'parse' from 'parsers' (unknown location)

所以现在固化成脚本，并在打包后**校验必检清单**，缺件直接非零退出。

用法：
    python build_context.py                 # 输出到 ../dist/grabber-build-context.tar.gz
    python build_context.py -o out.tar.gz   # 自定义输出路径
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE.parent / "dist" / "grabber-build-context.tar.gz"

# 不进包的东西。注意这里是**显式清单**，不是通配符——
# 用 `_*` 这类下划线通配符会误伤 `__init__.py`，这正是当初翻车的根因。
SKIP_DIRS = {
    "__pycache__", ".git", ".venv", "venv", ".idea", ".vscode",
    "node_modules", "data", "dist", "cloudflared",
}
SKIP_SUFFIX = (".pyc", ".pyo", ".log", ".tar", ".tar.gz")
SKIP_NAMES = {
    "config.json", "config.json.tmp", ".env",
    "start.bat", "install.bat", ".DS_Store", "Thumbs.db",
}

# 打包后必须存在的文件。少一个就别上传，省得又白跑一轮 Docker。
REQUIRED = [
    "Dockerfile",
    "server.py",
    "requirements.txt",
    ".dockerignore",
    "docker-compose.yml",
    "docker-compose.portainer.yml",
    "parsers/__init__.py",   # ← 就是它曾经被漏掉
    "parsers/base.py",
    "parsers/xhs.py",
    "parsers/douyin.py",
    "parsers/abogus.py",
    "parsers/web_api.py",
    "parsers/cookie_guard.py",
    "parsers/browser_login.py",
    "static/index.html",
]


def _skip(name: str) -> bool:
    if name in SKIP_NAMES or name.endswith(SKIP_SUFFIX):
        return True
    # 草稿/测试文件约定：单个下划线开头（如 _qr_test.py）。
    # 关键：必须排除 dunder（`__x`），否则会把 `__init__.py` 一起干掉——
    # 这正是用 `_*` 通配符翻车的原因。
    if name.startswith("_") and not name.startswith("__"):
        return True
    if name.startswith("__") and name not in ("__init__.py",):
        return True
    return False


def collect(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not _skip(d)]
        for fn in filenames:
            if _skip(fn):
                continue
            out.append(Path(dirpath, fn).relative_to(root))
    return sorted(out)


# ── .dockerignore 语义模拟 ────────────────────────────────────────────────
# 光看 tar 里有没有文件还不够：Docker 构建时还会再过一遍 .dockerignore。
# 当初的 `_*` 就是在这里把 parsers/__init__.py 吃掉的，所以必须把这一步也验一遍。
# 语义：不带斜杠的 pattern 按 basename 匹配（`_*` 因此能命中 `__init__.py`），
# 带斜杠的按相对路径匹配；`!` 前缀是例外，后匹配者优先。

def _read_dockerignore(root: Path) -> list[tuple[str, bool]]:
    p = root / ".dockerignore"
    if not p.exists():
        return []
    rules: list[tuple[str, bool]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        neg = s.startswith("!")
        if neg:
            s = s[1:].strip()
        s = s.rstrip("/")
        if s:
            rules.append((s, neg))
    return rules


def docker_ignored(rel: str, rules: list[tuple[str, bool]]) -> bool:
    rel = rel.replace(os.sep, "/")
    base = rel.rsplit("/", 1)[-1]
    ignored = False
    for pat, neg in rules:
        target = base if "/" not in pat else rel
        # fnmatchcase：避免 Windows 上大小写不敏感造成误判
        if fnmatch.fnmatchcase(target, pat):
            ignored = not neg
    return ignored


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    files = collect(HERE)
    with tarfile.open(out_path, "w:gz") as tf:
        for rel in files:
            tf.add(HERE / rel, arcname=str(rel).replace(os.sep, "/"))

    names = {str(p).replace(os.sep, "/") for p in files}

    print(f"输出: {out_path}")
    print(f"大小: {out_path.stat().st_size} 字节 / {len(files)} 个文件")
    print("\n清单:")
    for n in sorted(names):
        print(f"  {n}")

    print("\n必检清单:")
    missing = [r for r in REQUIRED if r not in names]
    for r in REQUIRED:
        print(f"  {'OK  ' if r in names else 'MISS'} {r}")

    leaked = [n for n in sorted(names)
              if n in {"config.json", ".env"} or n.endswith((".log", ".tar.gz"))]
    print(f"\n泄漏检查（应为空）: {leaked or '无'}")

    rules = _read_dockerignore(HERE)
    eaten = [r for r in REQUIRED if docker_ignored(r, rules)]
    print("\n.dockerignore 影响检查（必检文件不应被排除）:")
    for r in REQUIRED:
        bad = r in eaten
        print(f"  {'排除!' if bad else '保留 '} {r}")

    ok = True
    if missing:
        print("\n[失败] tar 中缺少必检文件，不要上传这个包！", file=sys.stderr)
        ok = False
    if eaten:
        print(f"\n[失败] .dockerignore 会把以下文件排除掉，镜像里同样会缺件：\n  " + "\n  ".join(eaten)
              + "\n  多半是用了下划线通配符（`_*` 会命中 `__init__.py`）。", file=sys.stderr)
        ok = False
    if not ok:
        return 1

    print("\n[通过] 构建上下文已就绪，可上传至 Portainer → Images → Build a new image。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

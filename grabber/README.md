# 素材抓取（小红书 / 抖音 → iPhone）

粘贴作品链接，解析出高清原图与无水印视频，在手机上直接保存。
解析服务可以跑在本机 Windows，也可以部署到 NAS（Docker / Portainer，见第七节），
再由 Cloudflare Tunnel 暴露成公网 https 域名，iPhone 随时可访问。

## 目录结构

```
grabber/
  server.py            FastAPI 服务：解析接口 + 图片/视频代理
  parsers/
    base.py            请求会话、UA、Cookie 配置与自动续期、JSON 提取
    cookie_guard.py    Cookie 保活 + 登录态探测（定时校验、失效即报）
    browser_login.py   手机扫码登录：在本机跑真实浏览器取 Cookie（可选）
    xhs.py             小红书解析（图文原图 / 视频 master 地址）
    douyin.py          抖音解析（有 Cookie 优先接口、无 Cookie 优先分享页，再退 yt-dlp/作品页）
    abogus.py          纯 Python 版抖音 a_bogus 签名（SM3+RC4+变体 Base64）
    web_api.py         抖音 web API 直连：a_bogus + 官方 ttwid，免登录取数据
  static/index.html    移动端网页
  config.json          Cookie 与代理配置
  start.bat            启动服务
  Dockerfile           容器镜像（默认轻量，不含浏览器）
  Dockerfile.browser   同上，但装了 Chromium（扫码登录可用）
  docker-compose.yml           含 build，适合 docker compose CLI / Portainer Git 仓库
  docker-compose.portainer.yml 不含 build，适合 Portainer Stack 的 Web 编辑器
  build_context.py    打包构建上下文 tar.gz，并校验必检清单（Portainer 上传用）
  .dockerignore       容器构建排除表（⚠️ 别用 `_*`，会误伤 __init__.py）
  data/                容器部署时的持久化目录（存 config.json，本机运行时不使用）

dist/
  grabber-build-context.tar.gz  构建上下文压缩包（Dockerfile 在包根），供 Portainer 上传构建镜像
```

## 一、安装与启动

```
install.bat     # 建虚拟环境并安装依赖（只需一次）
start.bat       # 启动服务，默认 http://0.0.0.0:8765
```

浏览器打开 http://127.0.0.1:8765 验证。同一 Wi-Fi 下的手机也能用 http://本机内网IP:8765 访问。

## 二、iPhone 上的保存方式

- **图片**：在图片上长按 → 「存储到相册」。点一下图片可放大，放大后同样长按保存。
- **视频**：点「下载到『文件』App」，再在文件 App 里打开视频 → 分享 → 存储到相册。
  iOS 不允许网页直接写入相册，视频必须多这一步。

建议用 Safari 打开后「分享 → 添加到主屏幕」，之后就像一个独立 App。

### 直接存到 NAS 本地目录（服务端落盘，不经过手机）

适合「在手机上挑图、在 NAS 上收素材」的 ComfyUI 工作流：解析出结果后，
**勾选**想要的图片/视频 → 点「下载选中到 NAS」，**服务端直接把文件写到 NAS 磁盘**，
手机只发指令、不传大文件。

- 落盘目录默认 **`/volume1/disk10/comfyui素材`**，**不再新建子目录**，所有图片/视频直接
  扁平落进该目录（方便 ComfyUI 直接批量读取，不用再进一层文件夹）。
- 文件名 = **`标题 + 序号 + 扩展名`**（如 `佳能阴天的清透感01.jpg`、`佳能阴天的清透感02.mp4`）：
  标题取自「标题前缀」输入框，默认自动填入笔记标题；留空则只保留序号（`01.jpg` …）。
  序号按勾选顺序 `01/02/03…` 自动编号，避免重名与路径穿越。
- 每个链接都过 SSRF 守卫（只放行小红书/抖音 CDN 域名），文件名只取「标题+序号+扩展名」，
  不会把 URL 里的路径写进磁盘。
- 目录可改：① 环境变量 `GRABBER_DOWNLOAD_DIR`（容器挂载场景优先）；② `config.json` 的
  `download_dir` 字段（经 `POST /api/config` 保存即时生效）；③ 都不设则用上面的默认值。
- **Docker / Portainer 部署务必把素材目录挂进容器**，否则服务端写不进去。本项目两份
  compose（`docker-compose.yml` / `docker-compose.portainer.yml`）**已内置**该挂载与
  `GRABBER_DOWNLOAD_DIR` 环境变量，直接重新部署即可，无需手动改：
  ```yaml
  volumes:
    - /volume1/disk10/comfyui素材:/volume1/disk10/comfyui素材
  environment:
    - GRABBER_DOWNLOAD_DIR=/volume1/disk10/comfyui素材
  ```
  （若你的 NAS 布局不同，把左侧宿主路径改成实际目录，并保持 `GRABBER_DOWNLOAD_DIR`
  等于容器内挂载点即可。）
- 接口：`POST /api/download` `{ urls:[...], kinds:[...], subdir:"可选标题前缀" }`，
  返回 `{ dir, saved, total, results:[{ok,file|error}] }`（`file` 为「标题+序号.扩展名」）。

## 三、免登录直连与画质说明

### 抖音（分享页 / a_bogus + ttwid，尽量免登录）

**现状（2026-09 实测）：抖音匿名取数已被风控大幅收紧。** 无 Cookie 时，
`/aweme/v1/web/aweme/detail/` 直接返 `403 Blocked by ArgusSecurityPlugin Uifid Not Found`，
分享页 SSR 也不再内嵌作品数据（`_ROUTER_DATA` 只剩页面元信息）。
→ **要稳定必须填已登录的 `douyin_cookie`**（登录态自带 `Uifid` / `msToken` / `s_v_web_id`），
其次给服务配一个干净的出口 IP。

解析顺序会**按有没有 Cookie 自适应**：有 Cookie 时优先打接口（最稳），无 Cookie 时优先分享页
（行为最像真人、相对不易触发滑块）；之后依次退到 yt-dlp 与作品页。
接口侧还做三件事压风控：**ttwid 注册一次缓存 6 小时**（旧版每个请求都重注册，是强机器特征）、
外呼间隔 ≥1 秒、补齐 `Origin` / `sec-fetch-*` 等浏览器头。

- 实现：`parsers/douyin.py`（编排）+ `parsers/web_api.py`（a_bogus + ttwid + 接口调用）
  + `parsers/abogus.py`（a_bogus 签名）。
- 依赖：`gmssl`（提供国密 SM3），已写入 `requirements.txt`。
- `a_bogus` 算法抖音会不定时更换版本；若某天直连突然返回空，多半是算法漂移，
  此时解析器会自动回退到分享页 / yt-dlp，不影响可用。

**画质策略（实测结论）：**

- **图片**：每张图同时给出 `url_list`（干净）与 `download_url_list`（带「抖音号：xxx」水印）。
  同一档位会下发 `.webp` / `.image` / `.jpeg` 多种编码——实测同为 2160×3240 时，`q75.webp`
  仅 163KB、高频细节最弱；`q75.jpeg` 有 451KB、边缘细节高约 14%，且 iOS 存图更友好。
  故**优先取干净的 `.jpeg`**；`download_url_list` 因带水印直接弃用。
  ⚠️ 两个坑（2026-09 实测）：
  1. **JSON 里的 `width`/`height` 不是实际像素**——分享页按原始尺寸的 **1/1.5** 上报
     （声明 1440×2160，实际下发 2160×3240）。所以别信元数据；界面会在图片加载完后用
     `naturalWidth/Height` 回写真实尺寸。
  2. `url_list` 里混着 `~tplv-dy-shrink:<w>:<h>` 的**预缩放档**（画质明显更低），必须剔除；
     否则它会被选成备用档，主档一失败就静默掉画质。变换段不能手改（签名绑定路径，改了必 403）。
- **视频**：把 `play_addr` / `play_addr_h264` / `play_addr_265` / `bit_rate[].play_addr` 统一收拢，
  按「分辨率 → 码率」降序取最高档（实测同一 1440×1080 视频：默认 h264 档 1.64MB，
  最高 h265 档 1.95MB、码率 +19%）。h265 档实测为 `hvc1` 封装，iOS / 相册可直接播放。
  前端另存一整条降级链：万一最高档播不了，`<video>` 会自动退到下一个码率档。

实现见 `parsers/base.py` 的 `douyin_image_pick()` 与 `douyin_video_candidates()`。

### 小红书（免登录拿真原图）

小红书网页默认只下发 `WB_DFT`（默认档，约 1080 宽，~150KB），直接拿永远只是预览级。
但图片的**原始对象键**形如 `notes_pre_post/<key>`，把任意一档的 CDN 地址
（如 `sns-webpic-qc.xhscdn.com/<时间戳>/<hash>/notes_pre_post/<key>!nd_dft_wlteh_jpg_3`）
去掉时间戳目录、鉴权 hash 与 `!nd_xxx` 后缀，拼回「图片 CDN」主机
`sns-img-hw.xhscdn.com` 并加动态缩放 `?imageView2/2/w/5000/format/jpg`，
即可**免登录**取回完整原图（实测：1080×1440 / 158KB → 3024×4032 / 766KB）。

实现见 `parsers/base.py` 的 `xhs_clean_original()` 与 `parsers/xhs.py` 的 `_image_entries()`。
每条图片都保留平台默认档地址作为自动兜底（前端图片加载失败时回退），无需任何 Cookie。

- 想要更高清/无水印：默认走上面的重建逻辑即可，不涉及登录。
- 填了 `xhs_cookie`：仅用于降低风控频率，非必需；解析器仍优先用重建出的真原图。

#### 小红书视频：网页端封顶 1080p，原画拿不到

图片能免登录重建原图，**视频不行**。实测（2026-09）：**无论是否登录**，网页端只下发三档转码流
`stream_types:[301,259,309]`，最高 **1080p、单档 ≤4.5MB**。42MB 级别的「原画」是未二次编码的
源文件，**只存在于小红书 App 的私签接口**（`edith` 移动端端点，需 App 签名）。

补测过、且全部失败的路：无签名 `feed API` → `406`；接上开源签名库 xhshow（web-only，
`xsec_appid=xhs-pc-web`）签 `POST /api/sns/web/v1/feed` → **200，但仍只有那三档**；
拿 web 签名去打移动端 `note/detail` → `404`；`discovery/item`、移动端 UA、匿名 `a1`、
按 `videoId`/`bizId`/`md5` 派生原始地址 → 全部拿不到。
（另：`xyw` 签名格式还会触发 `461 当前账号存在异常`。）

所以：

- 网页端能给的**最高就是 1080p**；填 `xhs_cookie` 相对匿名只解决「720p → 1080p」这一段，
  **不可能**到原画。要原画只能在 App 里手动保存。
- 解析器会把两个来源的档位合并后**按「分辨率 → 码率」取最高档**
  （`parsers/xhs.py` 的 `_collect_video_streams()`）。
- 当后端发现「笔记声明原始画质 > 实际取到的画质」时，会在页面顶部**如实说明**
  「网页端封顶、原画仅 App 提供」，不让你误以为已经是最清晰。

## 四、Cookie（填一次，长期有效）

**最简单的方式（网页内直接填，免改文件、免重启）：**

1. 打开页面，展开顶部的「⚙️ 登录态 Cookie」面板
2. 电脑 Chrome 登录 www.douyin.com（抖音）/ www.xiaohongshu.com（小红书）
3. F12 → Network → 任选一个请求 → Request Headers → 复制 `Cookie` 整行
4. 粘到对应输入框，点「保存并生效」——保存后会自动校验一次登录态（**只有小红书**能免签名判定，
   抖音没有对等手段），当场告诉你这个 Cookie 平台认不认

**两个框相互独立**：打开面板会自动预填当前**已保存**的值（明文仅对同源浏览器请求下发），
所以**只改其中一个再保存，不会把另一个清空**；某个框留空即表示「不修改该项」。
要清除请用「清空」按钮。解析请求**不再**携带输入框里的 Cookie（一律以已保存的配置为准，
以免绕过服务端刚接住的滚动续期 Cookie）。

**或手动改 `config.json`：** 把 Cookie 粘到 `xhs_cookie` / `douyin_cookie` 后重启服务。

### 为什么不需要反复手填

平台真正让登录态失效的，多数时候不是时间，而是这三件事——所以这三件都做了自动化：

| 风险 | 处理 |
|---|---|
| 风控 token 过期（`acw_tc` 只有 30 分钟） | 每次请求都接住响应里滚动下发的 `Set-Cookie`，自动合并写回 `config.json` |
| 会话长期不活动被回收 | 后台每 6 小时用当前 Cookie 打一次轻量页面保活 |
| 被登出 / 改密 / 异地登录 | 探测到就立刻在面板标红，并在解析结果里直接说清楚 |

面板上的状态行会显示：**登录态是否有效 / 上次校验时间 / 已自动续期多少次**。

**关于「自动读浏览器 Cookie」**：这条路在新版浏览器上已经走不通了。
Chrome 127+ 与新版 Edge 对 Cookie 库启用了 App-Bound 加密（实测本机 Edge 全部
`v20`，且 `Local State` 里存在 `app_bound_encrypted_key`），解密需要管理员权限 +
SYSTEM DPAPI + Chrome 身份校验；而且浏览器运行时还会独占锁住该文件。
所以本服务走的是「平台侧自动续期」而不是「本地解密」——前者零权限、可跨平台部署。

**保活间隔**可用环境变量调整：`COOKIE_KEEPALIVE_HOURS`（默认 `6`，设 `0` 关闭）。

### 过期 Cookie 不会让解析崩掉

实测发现：带一个**无效的登录态**请求小红书，它不会返回干净的「未登录」页面，而是直接
**500**。如果不处理，一个过期 Cookie 会让「本来免登录就能用」的解析整个失败——比不填还糟。
现在遇到这种情况会自动**退回匿名请求重试**，至少仍能拿到 720p，并在结果里提示你重新获取。

Cookie 只存在你本机，不上传任何服务器。

### 手机扫码登录（Cookie 直接落在服务所在机器上）

如果服务跑在 NAS 上，「从自己电脑复制 Cookie」其实不理想：那个 Cookie 里带着
**你电脑的设备指纹**（`a1`），却从 NAS 的 IP 发出去，容易显得不自然。更干净的做法是
**让服务自己所在的机器去登录** —— 页面上点「手机扫码登录」即可：

1. 服务在本机拉起一个真实浏览器，打开小红书登录页，把**二维码截图**回传到网页
2. 你用**手机小红书 App** 扫码并确认
3. 服务检测到登录成功，直接把 Cookie 写进本机配置（含这台机器自己的 `a1`）

全程不经过你的电脑，也不用复制任何东西。浏览器**只在登录那一刻存在**，
登录成功 / 取消 / 超时后立即关闭，日常解析仍然走轻量 HTTP。

**判据说明**：不能用「cookie 里出现了 `web_session` 或 `a1`」来判断登录成功——
实测访客只要打开一次登录页，小红书就会下发
`a1 / webId / websectiga / sec_poison_id / web_session` 一整套，带这套 cookie 去请求
平台仍然回答 `loggedIn: false`。所以判据只能是**问平台**：读 `__INITIAL_STATE__.user.loggedIn`。

**需要额外安装**（这是可选能力，默认镜像里没有）：
构建镜像时用 `Dockerfile.browser`，或用 `--build-arg INSTALL_BROWSER=1`。
代价是镜像约 +500MB（Chromium 及系统依赖）。只走粘贴 Cookie 的话不需要。

> 纯 HTTP 做不到这件事：实测小红书**匿名访问不再下发 `a1`**，
> 且所有 `/api/sns/web/v1/*` 都要 `x-s` 签名（无签名一律 406/500，
> 连 `login/qrcode/create` 都被网关拒掉）。所以「在 NAS 上原生拿 Cookie」
> 只有真实浏览器这一条路。

## 五、Cloudflare Tunnel

### 快速验证（随机域名，重启即变）

```
cloudflared tunnel --url http://127.0.0.1:8765
```

命令行会给出一个 `https://xxx.trycloudflare.com` 域名，手机直接访问即可。
适合先验证整条链路，不适合长期使用。

### 固定域名（推荐）

```
cloudflared tunnel login
cloudflared tunnel create grabber
cloudflared tunnel route dns grabber grabber.你的域名.com
```

填写 `cloudflared/config.yml`（参考 `config.yml.example`），然后：

```
cloudflared tunnel run grabber
```

Windows 装 cloudflared：`winget install Cloudflare.cloudflared`。

### ⚠️ 换了域名之后，只需要改一个地方

服务端**不存域名** —— 前端全部走相对路径、CORS 是 `*`、`config.json` 里没有域名字段。
所以换域名**不需要动任何代码或配置**。真正要改的只有：

| 要改的地方 | 说明 |
|---|---|
| **iOS 快捷指令**（必改） | 里面的地址是手打的，换域名后必须逐个改（见第六节两版） |
| Cloudflare 控制台 → 隧道 Public hostname | 换成新域名 |
| 浏览器书签 / 主屏幕图标 | 顺手改 |

**漏改快捷指令的典型症状**：点快捷指令后 Safari 显示 **`502 Bad Gateway`** 或白屏 ——
因为它还在打开那个**已经不存在的旧隧道域名**（服务端一切正常，所以很容易误判成「服务坏了」）。

> 随机 `trycloudflare.com` 域名**随时会死**（进程重启、隧道被回收），死了就是永久 `502`。
> 长期使用务必用固定域名，并且**换域名时一定要回来改快捷指令**。
>
> 自检顺序：① 手机浏览器直接打开新域名 → 页面能解析，说明服务端没问题；
> ② 服务端没问题而快捷指令没反应，那问题 100% 在快捷指令里的地址。

> 另注：Cloudflare 免费版对**单个响应体**有 100 MB 上限，
> 特别大的视频走 `/api/video` 下载可能失败（图片与常见短视频不受影响）。

## 六、用 iOS 快捷指令一键解析

页面支持查询参数自动解析：`?url=<链接或整段分享文案>`（也接受 `u` / `link`），
打开即自动填入并解析，解析完参数会从地址栏抹掉（刷新不会重复触发）。
因为解析入口本身支持整段分享文案，快捷指令直接把剪贴板原文塞进去即可，无需先抠 URL。

### 最简版：打开网页自动解析

1. 快捷指令 App → 新建 → 右上角 ⓘ → 勾选「在共享表单中显示」，输入类型选「URL」+「文本」。
2. 依次添加动作：
   - `从输入获取 URL`（无共享输入时改用 `获取剪贴板`）
   - `URL 编码`
   - `文本` → `https://你的域名/?url=` 接上一步结果
   - `打开 URL`
3. 在抖音/小红书「分享 → 复制链接」，再运行该快捷指令，打开即已解析好，长按图片保存。

### 全自动版：不开网页，直接进相册

1. 同上接收输入。
2. `获取 URL 内容`：URL = `https://你的域名/api/parse`，方法 `POST`，请求体 `JSON`，
   加一个字段 键 `url`、值 = 输入链接。
3. `从「获取 URL 内容」中获取字典值`，键填 `items`。
4. `重复每一个`（遍历 items）：
   - `获取字典值` 键 `url`
   - `URL 编码` → `文本`：`https://你的域名/api/img?u=` 接上结果
   - `获取 URL 内容` → `存储到「照片」相册`
5. 视频帖把 `/api/img?u=` 换成 `/api/video?u=`（末尾加 `&dl=1`），用 `存储文件` 存到「文件」App。

> 快捷指令里的域名必须是**固定**的（见下节固定域名）。用随机 `trycloudflare` 域名时每次重启都会变，快捷指令会失效。

## 七、部署到 NAS（Docker / Portainer）

### 为什么是 Docker，不是青龙面板

青龙面板是**定时任务面板**（跑签到脚本那一类），它不负责监听端口对外提供网页服务，
依赖也按脚本单独管理。本项目是**常驻 HTTP 服务**——要给 iPhone 打开网页、要暴露端口、
要挂反向代理/隧道，所以正确的载体是 **Docker**（群晖叫 Container Manager）。

（如果你另有一个需求是「每天定时批量下载」，那才轮得到青龙。）

### 0. 三种方式怎么选

| 方式 | 适合谁 | 是否现场构建 | 备注 |
| --- | --- | --- | --- |
| **Portainer** | 已有 Portainer 控制台 | ✗ 先单独构建镜像 | Stack 网页编辑器**不支持 `build:`** |
| 群晖 Container Manager | 群晖用户 | ✓ | 「项目」里直接粘 compose |
| SSH 命令行 | 会用终端 | ✓ | 最直接，`docker compose up -d --build` |

> **第一坑**：Portainer 的 Stack（Web editor）方式**没有构建上下文**，遇到 `build:` 会直接报错。
> 所以走 Portainer 必须**先把镜像构建出来**，再让 Stack 用 `image:` 引用它 —— 这正是下面方案 A 的两步。

### A. 用 Portainer 部署（推荐给你现在这套）

构建包已经生成好：`dist/grabber-build-context.tar.gz`（约 64 KB / 18 个文件，
**Dockerfile 在压缩包根目录**，不含 `config.json` / `.env`，可直接当构建上下文上传）。

要重新生成（**用脚本，别手搓 tar 命令**）：

```bash
python grabber/build_context.py
```

脚本会打完包后逐条校验「必检清单」，并模拟 `.dockerignore` 规则确认没有必检文件会被排掉——
缺件直接非零退出，省得又白跑一轮 Docker。**改动过 `parsers/` 或 `.dockerignore` 后请务必重跑。**

**第一步：构建镜像** —— Portainer → Images → **Build a new image**

1. Name 填 `grabber`，Tag 填 `latest`
2. 选 **Upload** → 选中 `grabber-build-context.tar.gz`
3. 点 **Build the image**，等它装完依赖（几分钟）

> 想用「手机扫码登录」，构建时加 `--build-arg INSTALL_BROWSER=1`（Portainer 的 Build 界面里有
> Build arguments 一栏，填 `INSTALL_BROWSER=1`）—— 镜像大约会大 +500 MB。
> 不需要扫码登录就别加，保持镜像小。

**第二步：部署 Stack** —— Portainer → Stacks → **Add stack**

1. Name 填 `grabber`
2. 编辑区**粘贴 `docker-compose.portainer.yml` 的全部内容** —— 这份文件没有 `build:`，
   用的是 `image: grabber:latest`，Cookie 存在命名卷 `grabber-data`
3. 点 **Deploy the stack**

完成后 `http://NAS内网IP:8765` 就能打开。

> #### 排错：`ImportError: cannot import name 'parse' from 'parsers' (unknown location)`
>
> **这是打包时漏了 `parsers/__init__.py`。** `unknown location` 是判据——Python 找到了
> `parsers/` 目录，但里面没有 `__init__.py`，于是按 PEP 420 把它当成「命名空间包」，
> 这个包能导入子模块、却导不出 `parse`。
>
> 根因通常是 `.dockerignore` 里用了 **`_*` 这类下划线通配符**：不带斜杠的 pattern
> 是按 **basename** 匹配的，`_*` 会连 `__init__.py` 一起排掉。
> 本仓库已改为显式列举草稿文件，并加了构建期自检，正常不该再出现。
> 若仍遇到：确认 `.dockerignore` 里没有裸的 `_*`，然后重新跑 `build_context.py` 再重建镜像。

### B. 用群晖 Container Manager「项目」

1. Container Manager → **项目** → 新增
2. 路径选 `/volume1/docker/grabber`，来源选「创建 docker-compose.yml」
3. 把本仓库的 **`docker-compose.yml`**（**这份含 `build:`，会现场构建**）内容粘进去 → 下一步 → 完成
4. 首次会现场构建镜像（装依赖，几分钟）；之后 `http://群晖内网IP:8765` 就能打开

> 群晖旧版 DSM 只有「Docker」套件、界面是英文的，操作一样：注册表/映像里选「新增 → 从文件添加」，
> 或用 SSH 走下面第 C 节。

### C. 或者 SSH 命令行

先在 NAS 上放源码：用 File Station 建目录，例如 `/volume1/docker/grabber`，把 `grabber/` 里的文件
全部传进去（`cloudflared/`、各种 `.log`、`.venv` 可以不传，镜像里用不上）。

Cookie 配置**不用手工建文件**：容器会把配置写到项目下的 `data/config.json`，目录不存在会自动创建。
想沿用本机已有的 Cookie，就直接在 NAS 上建 `data/` 目录、把本机 `config.json` 传进去改名即可。

```bash
cd /volume1/docker/grabber
sudo docker compose up -d --build
sudo docker compose logs -f grabber      # 看日志
sudo docker compose up -d --build        # 改完代码重新部署
sudo docker compose down                 # 停掉
```

### D. 更新 / 迁移

- **本地构建（群晖 / SSH）**：覆盖新文件，再 `sudo docker compose up -d --build`。
  `data/` 里的 Cookie 不受影响（`GRABBER_CONFIG` 已指向它）。
- **Portainer**：重建镜像（同名同 tag 直接覆盖旧镜像）→ 回到 Stack 点 **Update the stack** → 重新部署。
  命名卷 `grabber-data` 不动，Cookie 保住。
- **从本机搬到 NAS**：本机 `config.json` → NAS 的 `data/config.json`（Portainer 版则是容器内的
  `/app/data/config.json`，对应命名卷 `grabber-data`），服务启动即自动生效；
  也可以在网页的「Cookie 设置」里粘贴，效果一样。

### E. 外网访问：Cloudflare 命名隧道（固定域名）

不要再用 `cloudflared tunnel --url`（随机域名，重启即变）。改成命名隧道：

1. Cloudflare Zero Trust → Networks → Tunnels → 新建隧道
2. Public hostname 那里，Service 填 `http://grabber:8765`（容器名，同一 compose 网络内可直接解析）
3. 复制隧道 Token：
   - 群晖 / SSH：写进 NAS 上的 `.env` → `CF_TUNNEL_TOKEN=eyJh...`
   - Portainer：在 Stack 的 **Environment variables** 里加一条 `CF_TUNNEL_TOKEN=eyJh...`
4. 打开 `docker-compose.yml` / `docker-compose.portainer.yml` 里 `cloudflared:` 那几行的注释，再重新部署

这样域名固定，快捷指令就不会失效。

### F. 注意事项

- **Portainer 不支持 `build:`**（反复强调的第一坑）：Stack 只能用 `image:`。改代码时要先重建镜像再 Update stack。
- **端口冲突**：8765 被占用就改 `ports` 左边，例如 `"18765:8765"`。
- **架构**：`python:3.12-slim` 有 x86_64 与 arm64 两种，群晖两种机型都能构建。
  `curl_cffi` 少数架构没预编译 wheel，Dockerfile 里已做降级（退回 curl 兜底，功能不受影响）。
- **Cookie 持久化**：配置存在卷里 —— compose 版是 `data/config.json`，Portainer 版是命名卷
  `grabber-data` 里的 `/app/data/config.json`。重建容器 / 重新构建镜像都不会丢。
- **Cookie 保活**：`COOKIE_KEEPALIVE_HOURS`（默认 6 小时，设 0 关闭），两份 compose 里都已设。
  若 `curl_cffi` 在 NAS 架构上没装上，解析仍可用，但「自动接住滚动 Cookie」会失效
  （那条能力依赖 curl_cffi 能拿到响应 Cookie）。
- **手机扫码登录**：需要镜像里装了浏览器（构建时 `INSTALL_BROWSER=1`）。没装时该入口会自动隐藏/报错，
  这时只能在本机浏览器上手工复制 Cookie 填进设置里。
- **安全**：本服务**没有鉴权**，任何拿到域名的人都能用它解析、下载，`POST /api/config` 还能改
  `download_dir`（服务端任意路径落盘）。**`GET /api/config` 对「同源浏览器请求」会回传明文 Cookie**
  （用 `Sec-Fetch-Site` 做门槛，脚本 / 跨站拿不到），但这条门槛不是真鉴权。
  若挂公网，**强烈建议**在 Cloudflare 上加 Access 策略（邮箱验证），或至少用一个别人猜不到的
  路径前缀；也可把端口只绑内网：`"127.0.0.1:8765:8765"`。
- **代理**：NAS 上如果有科学上网代理，可在 compose 的 `environment` 里放开 `PROXY=`。

## 八、已知限制

- **抖音匿名解析已基本不可用**（2026-09）：接口返 `403 Uifid Not Found`、分享页不再内嵌数据。
  补已登录的 `douyin_cookie` 是最有效的解法，其次是换一个干净的出口 IP（代理）。
  同一出口 IP 短时间请求过密也会被上滑块——解析器已内置 ≥1 秒外呼间隔。
- **小红书视频网页端封顶 1080p / 单档 ≤4.5MB**，原画（约 42MB）仅 App 私签接口提供，网页侧拿不到。
- 小红书分享链接里的 `xsec_token` 有时效，过期的旧链接需重新复制。
- 服务所有外链资源都走本地代理，因此 https 页面不会出现混合内容拦截；代理仅放行小红书/抖音相关域名。
- 下载内容请仅用于个人学习或已获授权的用途，尊重原作者版权。

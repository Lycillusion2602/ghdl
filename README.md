# ghdl —— 通用下载器 + 格式转换（本地网页版）

粘贴一个链接就下，不管它是 GitHub release、公司内网导出、还是随便一个 http(s) 文件；
下完要转格式，图片 / PDF / Office / 音视频也在同一个页面里做。
Python 起一个只监听 `127.0.0.1` 的小服务，浏览器操作，发布版是单文件 exe，不用装 Python。

> **环境**：Windows 10/11（系统自带 curl）。直接下 exe 不需要 Python；
> 源码运行用 Python 3.10+（推荐 3.12），先 `pip install -r requirements.txt`。

## 1.2.2 更新了什么

- **下载**：任意 http(s) 直链、批量粘贴、下载前预检文件名/大小/网页、Referer/Cookie、断点继续、公司代理、Windows 原生目录选择框。
- **转换**：图片 / PDF / Office / 音视频；新增 **PNG → ICO**（16–256 多尺寸，保留透明，可只导一个尺寸）。
- **界面**：Vuexy 式左侧导航 + 顶部 navbar + stat cards；窄屏抽屉，表格自动变卡片。
- **图标**：exe 图标、网页 favicon、页头 logo 统一用下载箭头。
- **构建**：`requirements.txt` + `ghdl.spec` 进仓库，CI 装依赖后按 spec 打包。

## 怎么用

两条路，按需要选：

**只是要用** → 去 [Releases](https://github.com/Lycillusion2602/ghdl/releases) 下 `ghdl.exe`（单文件，33 MB 左右），放哪个文件夹都行，
**双击就开**：浏览器自动打开 `http://127.0.0.1:8765/`。不依赖 Python、不需要管理员权限、没有命令行窗口。
要停止就在**页面右上角按「停止服务」**。exe 没做代码签名，首次运行 Windows 会弹 SmartScreen
「未知发布者」——点「更多信息 → 仍要运行」即可，这是所有未签名单文件程序的正常表现；
Release 说明里附了 SHA256，可以先比对再跑。

**要改代码** → 先装依赖，再双击 `ghdl.bat`：

```
py -3 -m pip install -r requirements.txt
ghdl.bat
```

菜单里：

```
1  run      启动服务并打开浏览器（改完代码立刻生效，不用重新打包）
2  test     跑 selftest.py（110 多项，约 3 分钟，联网部分要能连 GitHub）
3  pack     本地打一个 ghdl.exe（会自动装依赖 + PyInstaller）
4  stop     让正在跑的服务退出
5  version  打印版本号
```

> 别同时开着 exe 和 `ghdl.bat 选 1`：两者共用同一个 `data/`（历史是同一份），
> 但第二个会漂到 8766 端口，你会看到两个页面各管一个服务，容易分不清。

首次运行会在同目录生成 `data/`（配置、代理列表、历史、日志、可选引擎），删掉它就是重置。
界面固定**可爱**风格（奶油粉 + 白卡片 + 胶囊输入框），想换风格改一行代码，见下面「风格与动效」。

## 下载：任意链接，不再只认 GitHub

- **GitHub**：release 文件、raw / blob 单文件、整仓 zip、仓库首页、发布页都认；
  贴已经套过加速源的链接也行（会先剥掉再重选源）。
- **非 GitHub**：默认**直连优先**，不再把 `公司内网/对象存储/网盘直链` 拼到 gh-proxy 后面白等。
  想让它们也走加速源，在「代理源 → 网络设置」里打开开关。
- **批量**：链接框一行一个，一次最多 50 条；同时下载数默认 3（`data/config.json` 的
  `max_concurrent`，改完重启生效）。
- **下载前预检**：非 GitHub 链接会先发一个 HEAD（不支持就退化成只取 1 字节的 Range 请求），
  提前拿到真实文件名、总大小和内容类型。返回 `text/html` 时会拦住，避免把登录页存成 `.exe`；
  确实是网页文件就勾「网页也照下」。
- **内网系统**：高级里可以填 `Referer`、`Cookie`、`Authorization`，也可以自己指定落盘文件名。
  这些值只放在当前任务的内存里，**不写历史、不写日志**。
- **断点续传**：失败/取消后如果还留着 `.part`，任务卡上会出现「继续」，沿用原落点和已下字节；
  同一次任务里换源也会续传，不会从头下。
- **下载目录**：
  - 「浏览…」直接弹 **Windows 原生选择框**（可以新建文件夹），不用在网页里从盘符一层层点；
  - 「目录树」是打不开原生框时的兜底，支持进目录、上一级、**新建文件夹**；
  - 「最近目录」会记住上次下到哪，下次打开页面默认就填它；常用目录仍可在 `config.json` 里配。
- **公司代理**：支持给 curl 指定 `proxy_url`（如 `http://127.0.0.1:7890`），
  内网只能走代理时不用去改系统设置。
- **校验照旧**：SHA256、zip 全量 CRC、exe 数字签名；GitHub release 还会去找作者公布的校验文件。

## 转换：图片 / PDF / Office / 音视频

选文件（可以多选）→ 选目标格式 → 开始转换，结果出现在任务列表里，同名文件自动加 `(2)`，绝不覆盖。

| 类别 | 能做什么 | 用的引擎 |
|---|---|---|
| 图片 | PNG / JPG / WebP / AVIF / HEIC / TIFF / BMP / GIF 互转；**PNG → ICO（16–256 多尺寸，保留透明，可只导一个尺寸）**；多张图合成一个 PDF；图片装进 PDF | Pillow + pillow-heif（内置） |
| PDF | 每页转 PNG/JPG/WebP；提取文字；合并多个 PDF；每页拆开；旋转；提取指定页；加密/解密；无损重写 | pypdf + pypdfium2（内置） |
| Office | doc/docx/odt/rtf/txt/html ↔ PDF；xls/xlsx/ods/csv ↔ PDF；ppt/pptx/odp ↔ PDF；同一家族内互转；Office 每页转图片 | 本机 LibreOffice |
| 音视频 | mp4 / mkv / webm / gif / mp3 / wav / m4a / flac / ogg 互转；视频抽音频；视频转 GIF | 本机 ffmpeg |
| 标记文档 | Markdown / HTML / EPUB / DOCX / RTF ↔ HTML / DOCX / EPUB / Markdown；Markdown 转 PDF | Pandoc（可选）+ LibreOffice |
| 矢量图 | SVG → PNG / JPG / PDF | ImageMagick（可选）或 LibreOffice |

页面上会实时显示引擎状态（绿色 ✓ / 红色 ✗）：

- **内置引擎**（Pillow / pillow-heif / pypdf / pypdfium2）已经打进 exe，换台电脑也有；
  源码运行需要 `pip install -r requirements.txt`。
- **LibreOffice** 和 **ffmpeg** 是外部程序，程序会自动在 PATH 和常见安装目录里找。
  本机装一个 LibreOffice 就能覆盖 Office 文档，装 ffmpeg 就能转音视频。
- **ImageMagick / Pandoc** 打红色 ✗ 时可以点「下载便携版」：
  程序会走自己的 GitHub 代理链路，从官方 release 下 zip 解压到 `data/engines/`，装完立即生效。
  也可以手动把便携版丢进 `data/engines/`，或在 `config.json` 的 `engines` 里写死路径。
- 转换是**先选文件再识别目标**：混选不同类别的文件会提示，一次只处理同一类。
- **PNG 转 ICO**：选好 PNG 后，「转成」列表第一个就是 `ICO（Windows 图标，多尺寸）`；
  默认生成 16/32/48/64/128/256 六个尺寸，也可以在「图标尺寸」里只选一个。
  非正方形图会补成透明正方形，透明背景保留；原图小于目标尺寸时不会硬放大。
- PDF 渲染、Office 多页转图片会一页一个文件（`名字-p01.png` 这种）。

**没做的**：OCR（扫描件取字）、PDF → Word 的高保真还原、视频剪辑。这三类要么需要 Tesseract/大模型，
要么体积和失败率都不适合塞进这个本地小工具；真需要时建议单独用专门的工具。

## 界面布局（参考 Vuexy 的 vertical layout）

只借结构，不引入 Vuexy 的代码/资源（参考它的 [vertical layout 说明](https://demos.pixinvent.com/vuexy-nextjs-admin-template/documentation/docs/guide/layout/vertical-layout-components/)）：

- **左侧导航**：品牌（图标来自 [static/logo.ico](static/logo.ico)，页头用 [static/logo.png](static/logo.png)）、分组（工作台 / 工具 / 记录）、菜单项；当前项高亮，任务在跑时「任务」旁边有数字角标。
- **顶部 navbar**：面包屑（随滚动更新）、服务状态、停止服务；窄屏变成汉堡菜单 + 抽屉。
- **概览**：四张 stat cards —— 进行中任务 / 今日下载 / 最近完成 / 可用引擎。
- **内容区**：下载与转换并排（窄屏自动堆叠），下面是任务、校验、代理源、历史。
- **窄屏表格**：代理源和历史从多列表格切成一条条卡片，不再需要横向滑动。
- exe 图标和网页 favicon 都来自 [static/logo.ico](static/logo.ico)，页头 logo 用 [static/logo.png](static/logo.png)。

界面固定**可爱**风格（奶油粉+白卡片+胶囊输入框）。想换风格是改一行代码，不是点按钮 —— 见下面「风格与动效」。

### 校验本地文件怎么用

填文件路径（或按「选文件…」在文件夹里点到它），想比对作者公布的哈希就把原来的下载链接也填上（选填），
按「开始校验」。结果出现在上面的任务列表里，标着「本地校验」。几 GB 的文件要等几十秒，期间可以取消。
这条**不写进历史**（历史只记下过东西，免得混进没下的条目），也不改动文件本身。

### 每步该看到什么（判据）

| 动作 | 正常的样子 |
|---|---|
| 双击 `ghdl.exe` | 没有窗口（正常，它是后台服务），一两秒后浏览器自动打开页面；要停就按页面右上角「停止服务」 |
| 按「停止服务」 | 没任务时立刻变「已停止」并置灰；有任务在跑时先拦住，让你再按一次「强制停止」 |
| 贴一个非 GitHub 直链 | 日志先出现「先探一下这个链接」，随后显示 `http=200 类型=... 大小=...`，只尝试**直连**一次就下完 |
| 一次贴多行链接 | 任务卡按行出现；同时最多 3 个在跑，其余「排队中」 |
| 按「浏览…」 | 弹 Windows 原生目录框，可新建文件夹；选完路径立刻填进输入框 |
| 选好图片文件 | 「转成」下拉框列出 ICO/WebP/PDF/AVIF 等；选 PNG 时 ICO 排第一 |
| 按「开始转换」 | 任务卡显示 `已完成 0 / N`，逐文件往上走；完成后出现产出文件按钮 |
| 缺 ImageMagick | 引擎行是红色 ✗，后面有「下载便携版」；点完任务列表出现「装引擎」，完成后变绿 |
| 某个源不行 | 日志里多一条「第 2 次尝试：<下一个源>」，不会从头重下（大文件会看到「从 X 处续传」） |
| 全部源都不行 | 徽标「失败」，日志逐条写明每次尝试的 http 码和 curl 退出码，任务卡上出现「继续」 |

## 支持贴哪些链接

全都直接原样粘，程序自己认：

```
https://github.com/作者/仓库/releases/download/v1.0/文件名.exe   release 里的某个文件
https://github.com/作者/仓库/raw/main/docs/a.md                  仓库里某个文件（blob 链接也认）
https://github.com/作者/仓库/archive/refs/heads/main.zip         整仓打包
https://github.com/作者/仓库                                      仓库首页 → 默认下整仓 zip；选「强制 git clone」就浅克隆
https://github.com/作者/仓库/releases                             发布页 → 按「列出该页文件」挑
https://gh-proxy.com/https://github.com/...                       已经套过代理的加速链接（会先剥掉）
https://任意站点/路径/文件名.pdf                                  非 GitHub 直链，直连优先
example.com/a.pdf                                                漏了 https 也认
```

## 数据放哪

全在 `data/` 里，纯文本，想改直接改（改完刷新页面即可，不用重启）：

- `proxies.json` —— 代理源列表（`prefix` 就是拼在完整地址前面的那段）
- `config.json` —— 默认目录、最近目录、端口、并发数、公司代理、UA、引擎路径、判卡死的阈值
- `history.json` —— 最近 300 条下载记录
- `server.log` —— 运行日志，出问题先看它
- `engines/` —— 按需下载的 ImageMagick / Pandoc 便携版（没装就是空的）

常用配置项：

| 键 | 含义 |
|---|---|
| `default_dir` | 没有最近目录时用的默认下载目录 |
| `last_dir` / `recent_dirs` | 上次落点 / 最近 8 个目录，界面自动填 |
| `max_concurrent` | 同时下载/转换几个，默认 3（改完重启生效） |
| `proxy_url` | 给 curl 的公司/系统代理，留空 = 不走 |
| `external_proxy` | 非 GitHub 链接是否也走加速源，默认 `false` |
| `user_agent` | 请求头里的 UA；有些内网系统会挑 |
| `engines` | 手动指定 `libreoffice` / `ffmpeg` / `magick` / `pandoc` 的 exe 路径 |
| `speed_floor_bps` / `speed_floor_secs` | 连续多少秒低于多少速度就判源卡死 |

想彻底删掉这个工具：把 `githubDownload` 整个文件夹删了就走，不留任何东西。

## 项目结构

| 文件 | 作用 |
|---|---|
| `server.py` | 本地 HTTP 服务、链接解析、选源、下载/续传、校验、任务状态机 |
| `converters.py` | 转换引擎适配：Pillow / pypdf / pypdfium2 + LibreOffice / ffmpeg + 可选 ImageMagick / Pandoc |
| `static/` | 前端（手写 HTML/CSS/JS，不引框架、不引 CDN） |
| `selftest.py` | 110+ 项自测（含离线转换和 exe 实测） |
| `ghdl.spec` | PyInstaller 构建配置（转换引擎 DLL 收集 + exe 图标） |
| `requirements.txt` | 源码运行 / 构建依赖 |
| `ghdl.bat` | 开发菜单（run / test / pack / stop / version） |

## 自测

改了代码之后双击 `ghdl.bat` 选 2（或本机命令行 `py -3 selftest.py`）。它会用临时目录和备用端口起一个独立实例，
不碰你真正的配置和历史，跑 110 多项检查：

- 语法、入口、版本一致性；
- 12+ 种链接形态、URL 解码、裸域名、非 GitHub 直连优先；
- **离线转换**：PNG→WebP、**PNG→ICO（多尺寸/透明/单尺寸）**、多图→PDF、PDF→PNG、LibreOffice HTML→PDF、同名不覆盖；
- 真下载、断线换源、挤牙膏源识别；
- 批量提交、失败继续、新建文件夹、格式识别、转换接口；
- 前端资源/主题/动画一致性、Vuexy 式侧边栏/顶栏结构、logo 与 favicon、前后端字段对齐；
- 目录里有 `ghdl.exe` 时，还会把 exe 起一遍：验它能起来、页面在包里、能下文件、
  **内置转换引擎能真的转出文件**、`data/` 落在 exe 旁边、停止按钮真能关进程。

**校园网抽风时它会老实说**：自测开头要先取一个参考文件（依次试 gh-proxy.com → ghfast.top → ghproxy.net → 直连，
每个重试两轮），全都取不到就只报前面离线检查的结论，然后写明 "联网部分测了也不可信，本轮跳过"，
退出码 3，**不会把网络问题报成代码的错**。离线转换部分仍然有效。

## 发版

1. 改 `server.py` 里的 `APP_VERSION`（版本号唯一出处，左侧栏底部显示的就是它）
2. `git commit` 并 `git push`
3. `git tag v1.2.2` 然后 `git push origin v1.2.2`
4. Actions 会：**校验 tag 和 `APP_VERSION` 一致** → 装依赖 → **跑完 selftest（不通过就不出包）** →
   `pyinstaller ghdl.spec` → 发 Release + SHA256

`ghdl.spec` 现在**进仓库**了：转换引擎带 DLL，必须显式 `collect_all`，构建参数不适合再散落在 CI 和 bat 两处。
想本地打一份：`ghdl.bat` 选 3。

## 已知边界（不做，说明白）

- 下载只监听 `127.0.0.1`，没有登录鉴权 —— 别把它改绑到 `0.0.0.0` 暴露到校园网。
- 不做多源分块并发下载；单源已经能跑到 30~45 MB/s，够快。
- 非 GitHub 链接默认只直连；有些公司的下载站必须带 Cookie/Referer，高级里能填，但不做浏览器登录态抓取。
- `.part` 续传沿用同一个任务；换一个新的下载任务仍会另起文件名（`(2)`）。
- 文件总大小拿不到时（少数源不分块返回），进度条用呼吸态表示"不知道还剩多少"。
- 取 release 文件列表要问 `api.github.com`，这个域名在这台机器上时好时坏；不通时就回落到"你自己右键复制直链"。
- 校验能证明"文件完整、签名有效、和作者公布的哈希一致"，不能证明"这个作者可信"。
- 转换是本地进程调用：LibreOffice/ffmpeg 缺失时对应格式不出现；ImageMagick/Pandoc 需要按需下载。
- 不做 OCR、PDF→Word 高保真、视频剪辑；不做批量转码队列的断点续转（转一半取消，已完成的文件保留）。

## 想自己搭一个不会失效的源

现成站子活不过几个月。想要长期稳定的，用自己的 Cloudflare Workers 部署 [hunshcn/gh-proxy](https://github.com/hunshcn/gh-proxy)：
把 `worker.js` 内容贴进 Workers 新建项目、部署、拿到 `https://xxx.workers.dev/`，
回到本工具「代理源」里添加它，测一下速度，就能一直用自己的那条路。免费额度对个人下载完全够。

## 风格与动效

界面固定用**可爱**风格：奶油粉背景、白卡片、胶囊输入框、大圆角、渐变页眉、悬停有粉色软阴影。
右上角不放主题切换器。

想换风格：改 [static/index.html](static/index.html) 顶部那行内联脚本里的一个词 ——

```js
document.documentElement.dataset.theme = 'cute';   // cute 可爱 / simple 简约 / dark 深色 / paper 护眼米色
```

四套的配色都在 `static/style.css` 开头各自的变量袋里，改颜色就改那一袋。
再加一套：复制一袋 `:root[data-theme="你的名字"]{...}`（36 个变量都得给齐，少一个自测报错），
主题专属外观写在「主题专属的外观」那段，**不要写死颜色到共用区**（自测有断言盯着）。

动效：卡片依次淡入、新任务淡入、下载中进度条反光横扫、总大小未知时呼吸、
完成时 tada、失败时 headShake、提示条从顶上落下、release 文件列表逐行缩放入场。
Windows 开了「显示中减少动画」时全部自动降级。

用的是 [animate.css v4.1.1](https://github.com/animate-css/animate.css) 的关键帧，但**不引 CDN**——
本工具是给"GitHub 打不开的时候"用的，页面绝不能依赖外部资源，自测里有一条专门扫前端有没有外部
`src/href/@import`。做法是把用到的几个动画摘进本地 `static/animate.css`（5.5 KB），
文件头写明出处，许可证全文随附在 `static/ANIMATE-LICENSE.txt`。
注意 animate.css v4 是 **Hippocratic License 2.1**（不是 MIT），所以出处和许可证文本都保留了。

## 许可

本项目 MIT，见 [LICENSE](LICENSE)。

打包内置的转换库：Pillow（MIT-CMU）、pillow-heif（BSD-3-Clause）、pypdf（BSD-3-Clause）、
pypdfium2（BSD-3-Clause / Apache-2.0）。LibreOffice / ffmpeg / ImageMagick / Pandoc 只以独立进程调用，
不随 exe 分发，各自的许可证按原项目执行。

`static/animate.css` 来自 animate.css v4.1.1，许可证为 Hippocratic License 2.1，
全文见 [static/ANIMATE-LICENSE.txt](static/ANIMATE-LICENSE.txt)。

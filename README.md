# 长江雨课堂自动刷课答题脚本

![version](https://img.shields.io/badge/version-1.1.0-blue)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![license](https://img.shields.io/badge/license-GPL--3.0-green)

一个针对 **长江雨课堂**（`changjiang.yuketang.cn`）的自动化学习工具。

**原生桌面版：** Windows 软件入口为 `desktop.py`，启动与打包方法见 [桌面版说明](DESKTOP.md)。现有命令行入口保持可用。

- 🎬 **视频自动刷课**：模拟播放器心跳事件上报学习进度，无需真实播放视频
- 📖 **图文自动打卡**：自动标记课件为已读
- 💬 **讨论区自动发帖**：按配置调用 DeepSeek API 或 Codex 生成讨论内容并自动发布
- 📝 **作业 / 考试自动答题**：支持 DeepSeek API / Codex 两种模式，支持单选、多选、判断、填空、简答
- 🔐 **扫码登录**：自动获取并保存登录凭证
- 🔤 **加密字体解密**：还原雨课堂自定义字体加密的题目文本

> 📋 版本更新记录见 [CHANGELOG.md](CHANGELOG.md)

> ⚠️ **免责声明**：本项目仅供学习与技术研究使用，请勿用于任何商业用途或违反平台规则的行为。
> 使用本工具产生的一切后果由使用者自行承担。

---

## 目录结构

```
changjiangyu/
├── main.py                 # 程序入口
├── requirements.txt        # 依赖清单
├── .env.example            # 配置模板（复制为 .env 后填写）
├── yuketang/
│   ├── config.py           # 配置管理
│   ├── logger.py           # 日志
│   ├── session.py          # HTTP 会话（重试 / 限流处理）
│   ├── login.py            # 登录（扫码 / Cookie）
│   ├── course.py           # 课程与章节 API
│   ├── video.py            # 视频心跳刷课
│   ├── richtext.py         # 图文 / 讨论
│   ├── decrypt.py          # 加密字体解密
│   ├── ai.py               # DeepSeek / Codex 答题与模式选择
│   ├── homework.py         # 作业 / 考试
│   └── cli.py              # 命令行入口
└── data/                   # 运行时生成的字体缓存（自动下载，不入库）
```

---

## 快速开始

### 1. 环境要求

- Python **3.9+**
- AI 二选一：DeepSeek API Key，或本机已安装并登录的 Codex CLI（Codex 模式无需单独的 API Key）

### 2. 安装

在 Windows PowerShell 中运行：

```powershell
cd F:\Experimental-project\YuKeTang\yuketang-auto-script
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 仅首次配置时复制，已有 .env 请直接编辑，避免覆盖登录凭证
Copy-Item .env.example .env
```

后续命令里的 `python` 可以替换为 `.\.venv\Scripts\python.exe`，无需激活虚拟环境。

### 3. 配置

编辑 `.env`，填写必要项：

```ini
# 学校 ID（登录后可从浏览器 Cookie 中获取）
UNIVERSITY_ID=

# 登录凭证：推荐用扫码登录，可留空
CSRF_TOKEN=
SESSION_ID=

# 模式：deepseek 或 codex，默认 deepseek（原项目 API 模式）
AI_MODE=deepseek

# DeepSeek API（仅 deepseek 模式使用）
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_TEMPERATURE=0.3
DEEPSEEK_TIMEOUT=300

# Codex（仅 codex 模式使用，复用本机登录）
CODEX_CLI=codex
CODEX_MODEL=
CODEX_TIMEOUT=300
```

切换模式只需修改 `AI_MODE`，保存后重新运行命令。仅使用选中模式的配置，不会自动回退到另一个后端。
系统环境变量优先于 `.env`；若修改文件后仍使用旧值，请检查终端是否设置了同名环境变量。

使用 DeepSeek 时设置 `AI_MODE=deepseek` 并填写自己的 API Key。保留原项目的
`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`DEEPSEEK_TEMPERATURE` 配置，
模型名请参考 [DeepSeek 官方模型列表](https://api-docs.deepseek.com/quick_start/pricing/)，可手动修改。
SDK 仅在调用 DeepSeek 时加载，Codex 模式不要求填写 DeepSeek 密钥。

使用 Codex 时设置 `AI_MODE=codex`，先在终端验证登录：

```powershell
codex login status
# 未登录时运行 codex login，使用与桌面端相同的账号完成浏览器登录
.\.venv\Scripts\python.exe main.py ai-check
```

`ai-check` 按 `AI_MODE` 检查所选后端，只测试 `1 + 1`，无需雨课堂登录，也不会提交作业。
它会实际调用 AI，使用对应的 API 余额或 Codex 额度。
如果 `codex` 不在 PATH 中，在 `.env` 的 `CODEX_CLI` 填写 `codex.exe` 的完整路径。
本机桌面端自带程序的位置为 `D:/Application/Codex/app/resources/codex.exe`。
可用 `& "D:/Application/Codex/app/resources/codex.exe" login status` 检查登录。

`CODEX_MODEL` 留空时使用 Codex CLI 配置的默认模型；桌面端当前聊天的模型选择和聊天记录不会自动传入。
每道题独立调用一次 Codex，会使用账号的 Codex 额度。调用方式参见
[OpenAI 官方非交互模式文档](https://developers.openai.com/codex/noninteractive)。

> 💡 **如何获取 `UNIVERSITY_ID`**：浏览器登录雨课堂后，按 `F12` → `Application` → `Cookies`，找到 `university_id` 字段。

### 4. 登录

首次使用推荐扫码登录（自动保存凭证，后续无需重复）：

```bash
python main.py login
```

程序会生成二维码图片，并尝试用系统图片查看器打开。用绑定雨课堂账号的微信打开「扫一扫」，扫描电脑上的二维码，再按手机提示确认登录。

如果图片没有自动打开，查看终端打印的「二维码文件：」路径，手动打开该 PNG 图片扫码。扫码等待时间为 3 分钟，超时后重新运行命令。

终端出现「扫码登录成功」后，凭证会保存到 `.env`，之后执行其他命令会自动复用；凭证失效时会重新扫码。需要主动重新扫码时运行 `python main.py login --qrcode`。

扫码前需要按上文说明从浏览器 Cookie 获取学校 ID，填写到 `.env` 的 `UNIVERSITY_ID`（或桌面端「设置 → 连接 → 学校 ID」）后再登录。扫码不会自动获取学校 ID。

> 也可以手动把浏览器的 `csrftoken` / `sessionid` 填到 `.env`，然后运行 `python main.py login` 验证。

---

## 使用方法

### 查看课程

```bash
python main.py courses
```

### 刷视频

```bash
python main.py video                 # 刷所有课程
python main.py video -c 科研伦理     # 只刷名称含「科研伦理」的课程
python main.py video -s 2.0          # 自定义倍速（默认 1.5，过高易风控）
python main.py video -w 3            # 自定义并发数（默认 2）
```

### 图文打卡

```bash
python main.py richtext
```

### 讨论区发帖

```bash
python main.py discussion
```

### 作业 / 考试

```bash
python main.py homework              # 只做作业
python main.py homework --exam       # 同时做考试（风险较高，谨慎）
```

### 一键完成全部

```bash
python main.py all
```

`all` 默认执行视频与图文。讨论、作业、考试是否执行取决于 `.env` 中的 `AUTO_DISCUSSION`、`AUTO_HOMEWORK`、`AUTO_EXAM`，默认均为 `false`；需要包含对应功能时改为 `true`。
直接运行 `discussion` 或 `homework` 子命令时，无需开启对应的 `AUTO_*` 开关。

当前 `homework` 会生成答案并直接提交，`HOMEWORK_CONFIRM` 尚未实现人工确认。

### 全局参数

```bash
python main.py --debug <子命令>      # 打印调试日志
python main.py --quiet <子命令>      # 仅输出警告与错误
```

### 停止运行

在运行程序的终端中按 `Ctrl+C`，程序会停止当前 Codex 调用或取消视频并发任务并退出。若有已发出的网络请求，需要等待其返回或超时；中断后不再发送后续请求或重试。更新前已启动的旧进程需要先停止，再重新启动才能使用修复后的中断逻辑。

若终端仍无响应，另开一个 PowerShell 窗口，运行以下命令停止本项目通过 `.venv` 启动的任务及其子进程：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "F:\Experimental-project\YuKeTang\yuketang-auto-script\stop.ps1"
```

脚本按本项目虚拟环境的完整路径识别任务，不会结束其他项目的 Python 或 Codex 桌面程序。停止后，已提交到雨课堂的答案仍会保留。

---

## 配置项说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `YUKETANG_DOMAIN` | `changjiang.yuketang.cn` | 站点域名，一般无需修改 |
| `UNIVERSITY_ID` | — | 学校 ID |
| `CSRF_TOKEN` / `SESSION_ID` | — | 登录 Cookie（扫码登录后自动写入） |
| `AI_MODE` | `deepseek` | `deepseek` 使用原项目 API 模式，`codex` 使用本机 CLI；修改后重新运行命令 |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek API 密钥，仅 deepseek 模式使用 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容 API 地址 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | API 模型 ID，可按账号可用模型修改 |
| `DEEPSEEK_TEMPERATURE` | `0.3` | 保留原项目配置；各题型沿用原温度，思考模式可能忽略温度 |
| `DEEPSEEK_TIMEOUT` | `300` | 每次 API 调用超时（秒），临时错误最多尝试 3 次 |
| `CODEX_CLI` | `codex` | Codex 命令或可执行文件完整路径 |
| `CODEX_MODEL` | 空 | 留空使用 Codex CLI 默认模型，也可指定账号可用的模型 |
| `CODEX_TIMEOUT` | `300` | 单道题调用超时（秒），失败最多重试 3 次 |
| `VIDEO_SPEED` | `1.5` | 播放倍速（1.0 ~ 2.0，过高易风控） |
| `HEARTBEAT_INTERVAL` | `15` | 心跳批量提交间隔（秒，至少 5 秒）；每 5 秒采样一次播放进度 |
| `MAX_CONCURRENT_VIDEOS` | `2` | 并发刷课线程数（1 ~ 4） |
| `SKIP_COMPLETED` | `true` | 跳过服务端已标记完成或观看比例达到 100% 的视频 |
| `MAX_RETRIES` | `3` | 请求重试次数 |
| `AUTO_RICHTEXT` | `true` | 自动图文打卡 |
| `AUTO_DISCUSSION` | `false` | 自动讨论发帖 |
| `AUTO_HOMEWORK` | `false` | 自动做作业 |
| `AUTO_EXAM` | `false` | 自动做考试 |
| `HOMEWORK_CONFIRM` | `true` | 保留配置，当前尚未实现人工确认，答案会直接提交 |
| `TEST_MODE` | `false` | 仅限制处理前 N 个任务，仍会实际执行和提交 |

---

## 工作原理

- **视频刷课**：向雨课堂的 `/video-log/heartbeat/` 接口上报播放事件。`playing` 只用于开始播放，播放中每 5 秒记录一次 `heartbeat`，按配置间隔批量提交；普通事件的 `fp` / `tp` 为 0。程序会先读取服务端视频时长，并以服务端返回的观看区间和完成状态核实结果。
- **加密字体解密**：雨课堂部分题目使用子集化的思源黑体加密，程序会下载参考字体并逐字符渲染比对（IoU 匹配），还原明文。
- **智能答题**：将题目文本与选项拼装为 Prompt，按 `AI_MODE` 选择 DeepSeek 的 OpenAI 兼容 API 或本机已登录的 `codex exec`，共用题型提示词、答案解析和内存缓存，再通过作业提交接口回传。两种模式不会互相回退。

---

## 常见问题

**Q：如何切换 DeepSeek 和 Codex？**
在 `.env` 中设置 `AI_MODE=deepseek` 或 `AI_MODE=codex`，填写对应配置后重新运行命令。
用 `python main.py ai-check` 检查所选模式。DeepSeek 模式无需安装 Codex，Codex 模式无需 DeepSeek API Key。

**Q：Codex 提示未找到命令、未登录或超时？**
在 `.env` 中设置 `CODEX_CLI` 的完整路径，用该程序执行 `login status`；如未登录则执行 `login`。
用 `python main.py ai-check` 单独检查模型连接。慢题可增大 `CODEX_TIMEOUT`；额度不足时按 Codex 的提示处理。

**Q：登录失败 / 提示 Cookie 失效？**
Cookie 有有效期。重新运行 `python main.py login --qrcode` 扫码登录即可。

**Q：视频完成率不足 100%？**
先区分日志中的「上报播放位置」和「服务端完成率」：播放位置到结尾不代表整段视频已经被服务端记录。
已修复每次心跳重新生成播放会话 ID、误把最后播放位置当作完整观看区间，以及未完成也计为成功的问题。
同时已修正周期事件类型和拖动字段：周期上报必须使用 `heartbeat`，不能反复用 `playing` 代替；普通播放的 `fp` / `tp` 不应填入播放位置。
重新运行 `python main.py video -c "课程关键词"`，程序会读取服务端已观看区间，从首个未覆盖位置补看。
服务端进度未增加时会停止自动重放并计为失败；不会持续向结尾空发心跳。
只有日志出现「服务端确认完成」才计入成功。`video` / `all` 若仍有未完成的视频，会返回非零退出码。

**Q：题目文字乱码？**
首次解析字体需要联网下载参考字体（约 20MB），请确保网络可达 GitHub；之后会使用本地缓存。

**Q：考试提示「尚未开放」？**
雨课堂考试有开放时间限制，未到开放时间会自动跳过，到点后再运行即可。

**Q：提示「Expected available in X seconds」？**
这是雨课堂的限流机制，工具会自动等待并重试，无需干预。

---

## 风险提示

1. **请勿泄露 `.env`**：其中包含登录 Cookie，仓库已通过 `.gitignore` 排除，提交前请再次确认。
2. **控制频率**：过高的倍速、并发或过短的心跳间隔可能触发平台风控，建议保持默认配置。
3. **谨慎使用考试自动答题**：考试通常计分且规则严格，`AUTO_EXAM` 默认关闭。
4. 不要分享本机 Codex 登录凭证；需要退出时运行 `codex logout`。

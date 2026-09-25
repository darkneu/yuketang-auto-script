# 长江雨课堂自动刷课答题脚本

一个针对 **长江雨课堂**（`changjiang.yuketang.cn`）的自动化学习工具。

- 🎬 **视频自动刷课**：模拟播放器心跳事件上报学习进度，无需真实播放视频
- 📖 **图文自动打卡**：自动标记课件为已读
- 💬 **讨论区自动发帖**：调用 DeepSeek 生成讨论内容并自动发布
- 📝 **作业 / 考试自动答题**：调用 DeepSeek 智能作答，支持单选、多选、判断、填空、简答
- 🔐 **扫码登录**：自动获取并保存登录凭证
- 🔤 **加密字体解密**：还原雨课堂自定义字体加密的题目文本

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
│   ├── ai.py               # DeepSeek 答题
│   ├── homework.py         # 作业 / 考试
│   └── cli.py              # 命令行入口
└── data/                   # 运行时生成的字体缓存（自动下载，不入库）
```

---

## 快速开始

### 1. 环境要求

- Python **3.9+**
- 一个 [DeepSeek](https://platform.deepseek.com/) 账号（用于智能答题，仅刷视频可跳过）

### 2. 安装

```bash
# 克隆仓库
git clone <你的仓库地址>
cd changjiangyu

# 创建虚拟环境（推荐）
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt

# 复制配置模板
cp .env.example .env
```

### 3. 配置

编辑 `.env`，填写必要项：

```ini
# 学校 ID（登录后可从浏览器 Cookie 中获取）
UNIVERSITY_ID=

# 登录凭证：推荐用扫码登录，可留空
CSRF_TOKEN=
SESSION_ID=

# DeepSeek（仅智能答题时需要）
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx
DEEPSEEK_MODEL=deepseek-v4-pro
```

> 💡 **如何获取 `UNIVERSITY_ID`**：浏览器登录雨课堂后，按 `F12` → `Application` → `Cookies`，找到 `university_id` 字段。

### 4. 登录

首次使用推荐扫码登录（自动保存凭证，后续无需重复）：

```bash
python main.py login
```

终端会显示二维码，用 **长江雨课堂微信小程序 / 雨课堂 App** 扫码即可。

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

### 全局参数

```bash
python main.py --debug <子命令>      # 打印调试日志
python main.py --quiet <子命令>      # 仅输出警告与错误
```

---

## 配置项说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `YUKETANG_DOMAIN` | `changjiang.yuketang.cn` | 站点域名，一般无需修改 |
| `UNIVERSITY_ID` | — | 学校 ID |
| `CSRF_TOKEN` / `SESSION_ID` | — | 登录 Cookie（扫码登录后自动写入） |
| `DEEPSEEK_API_KEY` | — | DeepSeek API Key |
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` | 答题模型，可选 `deepseek-flash` |
| `DEEPSEEK_TEMPERATURE` | `0.3` | 答题温度，越低越稳定 |
| `VIDEO_SPEED` | `1.5` | 播放倍速（1.0 ~ 2.0，过高易风控） |
| `HEARTBEAT_INTERVAL` | `15` | 心跳发送间隔（秒） |
| `MAX_CONCURRENT_VIDEOS` | `2` | 并发刷课线程数（1 ~ 4） |
| `SKIP_COMPLETED` | `true` | 跳过完成率 ≥ 90% 的视频 |
| `MAX_RETRIES` | `3` | 请求重试次数 |
| `AUTO_RICHTEXT` | `true` | 自动图文打卡 |
| `AUTO_DISCUSSION` | `false` | 自动讨论发帖 |
| `AUTO_HOMEWORK` | `false` | 自动做作业 |
| `AUTO_EXAM` | `false` | 自动做考试 |
| `HOMEWORK_CONFIRM` | `true` | 提交作业前人工确认 |
| `TEST_MODE` | `false` | 测试模式，只处理前 N 个任务 |

---

## 工作原理

- **视频刷课**：向雨课堂的 `/video-log/heartbeat/` 接口上报 `playing` / `videoend` 等心跳事件，模拟真实播放。程序会先从服务端进度接口读取**真实视频时长**（`video_length`），避免因本地时长解析错误导致完成率不足。
- **加密字体解密**：雨课堂部分题目使用子集化的思源黑体加密，程序会下载参考字体并逐字符渲染比对（IoU 匹配），还原明文。
- **智能答题**：将题目文本与选项拼装为 Prompt，调用 DeepSeek 分步推理后按格式解析答案，再通过作业提交接口回传。

---

## 常见问题

**Q：登录失败 / 提示 Cookie 失效？**
Cookie 有有效期。重新运行 `python main.py login --qrcode` 扫码登录即可。

**Q：视频完成率不足 100%？**
多为网络波动导致部分心跳被丢弃。程序已内置「补发心跳」逻辑，重新运行 `python main.py video` 会自动从上次进度续看补齐。

**Q：题目文字乱码？**
首次解析字体需要联网下载参考字体（约 20MB），请确保网络可达 GitHub；之后会使用本地缓存。

**Q：考试提示「尚未开放」？**
雨课堂考试有开放时间限制，未到开放时间会自动跳过，到点后再运行即可。

**Q：提示「Expected available in X seconds」？**
这是雨课堂的限流机制，工具会自动等待并重试，无需干预。

---

## 风险提示

1. **请勿泄露 `.env`**：其中包含 API Key 与登录 Cookie，仓库已通过 `.gitignore` 排除，提交前请再次确认。
2. **控制频率**：过高的倍速、并发或过短的心跳间隔可能触发平台风控，建议保持默认配置。
3. **谨慎使用考试自动答题**：考试通常计分且规则严格，`AUTO_EXAM` 默认关闭。
4. 若发现 API Key 已泄露，请立即到 DeepSeek 平台吊销并重新生成。

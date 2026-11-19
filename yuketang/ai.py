# -*- coding: utf-8 -*-
"""DeepSeek / Codex 智能答题模块。

按 AI_MODE 选择 DeepSeek API 或本机已登录的 Codex CLI，支持：
- 选择题（单选 / 多选）
- 判断题
- 填空题
- 简答题 / 讨论区发帖

针对不同题型使用不同的 system prompt，并对模型输出做严格清洗，
确保返回格式可直接提交给雨课堂接口。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .config import Config
from .logger import get_logger

logger = get_logger("ai")

# ----------------------------------------------------------------------
#  System Prompts
# ----------------------------------------------------------------------
PROMPT_CHOICE = (
    "你是一位学识渊博、治学严谨的答题专家。请仔细阅读题目和选项，"
    "逐步分析每个选项的正确性，然后给出最终答案。\n"
    "分析要求：\n"
    "1. 逐项判断每个选项是否符合题意、是否符合学科常识与教材结论。\n"
    "2. 特别注意绝对化表述（如“所有”“一定”“必然”“只”“仅”）往往是错误项。\n"
    "3. 多选题需确认所有正确项都已选出，不要遗漏。\n"
    "输出格式（必须严格遵守）：\n"
    "先输出你的简要分析，最后另起一行，用如下格式给出答案：\n"
    "答案：A\n"
    "（多选题示例：答案：A, C）\n"
    "注意：最后一行必须且只能包含“答案：”加选项字母，不要有其他内容。"
)

PROMPT_JUDGEMENT = (
    "你是一位学识渊博、治学严谨的答题专家。请判断下面陈述的正误。\n"
    "分析要求：\n"
    "1. 逐句核对陈述是否符合学科常识与教材结论。\n"
    "2. 特别注意绝对化表述（如“所有”“一定”“必然”“只”“仅”“完全”）往往是错误项。\n"
    "输出格式（必须严格遵守）：\n"
    "先输出你的简要分析，最后另起一行，用如下格式给出结论：\n"
    "答案：正确\n"
    "或\n"
    "答案：错误\n"
    "注意：最后一行必须且只能包含“答案：”加“正确”或“错误”，不要有其他内容。"
)

PROMPT_FILL_BLANK = (
    "你是一个严谨的答题助手。请回答下面的填空题。\n"
    "规则：\n"
    "1. 严格返回 JSON 对象，键为空的序号（从 1 开始，字符串形式），值为该空的答案。\n"
    "2. 例如有 2 个空，返回 {\"1\": \"答案一\", \"2\": \"答案二\"}。\n"
    "3. 只返回 JSON，不要输出任何解释或 markdown 代码块标记。"
)

PROMPT_SHORT_ANSWER = (
    "你是一个大学生/研究生，请回答下面的简答题。\n"
    "规则：\n"
    "1. 内容详略得当、逻辑连贯，约 200-400 字。\n"
    "2. 直接输出答案正文，不要使用 markdown 语法（不要出现 #、*、$ 等符号）。\n"
    "3. 不要出现多余空行，不要自称「大学生」或「研究生」。"
)

PROMPT_DISCUSSION = (
    "你是一个大学生/研究生，请回答下面的讨论问题。\n"
    "规则：\n"
    "1. 以自然、真诚的口吻作答，约 300 字。\n"
    "2. 内容详略得当且连贯，不官方死板，不举尴尬的例子。\n"
    "3. 直接输出答案正文，不要使用 markdown 语法，不要有多余空行。"
)

# 题型常量
TYPE_CHOICE = "Choice"
TYPE_SINGLE_CHOICE = "SingleChoice"
TYPE_MULTI_CHOICE = "MultipleChoice"
TYPE_JUDGEMENT = "Judgement"
TYPE_FILL_BLANK = "FillBlank"
TYPE_SHORT_ANSWER = "ShortAnswer"
TYPE_DISCUSSION = "Discussion"


class AIError(Exception):
    """AI 调用异常。"""


class AIClient:
    """两种后端共用的题型提示词、解析和内存缓存。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._cache: Dict[str, Any] = {}

    @property
    def available(self) -> bool:
        raise NotImplementedError

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        *,
        temperature: Optional[float] = None,
    ) -> str:
        raise NotImplementedError

    # ------------------------------------------------------------------
    @staticmethod
    def _strip_thinking(text: str) -> str:
        """移除推理模型输出的 ``<think>...</think>`` 段落。"""
        cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        cleaned = re.sub(r"```(?:json)?\s*(.*?)```", r"\1", cleaned, flags=re.DOTALL)
        return cleaned.strip()

    # ==================================================================
    #  题型专用方法
    # ==================================================================
    def answer_choice(self, body: str, options: List[str], multiple: bool = False) -> str:
        """回答选择题，返回如 ``A`` 或 ``A, C``。"""
        prompt = self._format_question(body, options)
        if multiple:
            prompt += "\n\n注意：本题为多选题。"
        raw = self.chat(PROMPT_CHOICE, prompt, temperature=0.1)
        return self._normalize_choice(raw, multiple)

    # ------------------------------------------------------------------
    def answer_judgement(self, body: str) -> str:
        """回答判断题，返回 ``true`` 或 ``false``。"""
        raw = self.chat(PROMPT_JUDGEMENT, body, temperature=0.1)

        # 优先解析“答案：正确/错误”标记行，避免分析文字干扰
        marker = re.search(
            r"(?:答案|结论|判断)\s*[:：]?\s*(正确|错误|对|错|true|false)",
            raw,
            flags=re.IGNORECASE,
        )
        segment = marker.group(1) if marker else raw
        segment = segment.lower()

        # 先判“错误”，避免“不对/不正确”中的“对/正确”造成误判
        if "false" in segment or "错误" in segment or "错" in segment or "不对" in segment:
            return "false"
        if "true" in segment or "正确" in segment or "对" in segment:
            return "true"
        return "true"

    # ------------------------------------------------------------------
    def answer_fill_blank(self, body: str, blank_count: int = 1) -> Dict[str, str]:
        """回答填空题，返回 ``{"1": "答案", ...}``。"""
        prompt = (
            f"{body}\n\n本题共有 {blank_count} 个空，"
            f"请返回包含 {blank_count} 个键的 JSON 对象。"
        )
        raw = self.chat(PROMPT_FILL_BLANK, prompt, temperature=0.1)
        return self._parse_fill_blank(raw, blank_count)

    # ------------------------------------------------------------------
    def answer_short(self, body: str) -> str:
        """回答简答题。"""
        return self.chat(PROMPT_SHORT_ANSWER, body, temperature=0.7)

    # ------------------------------------------------------------------
    def answer_discussion(self, topic: str) -> str:
        """回答讨论区话题。"""
        return self.chat(PROMPT_DISCUSSION, topic, temperature=0.8)

    # ==================================================================
    #  通用入口
    # ==================================================================
    def answer(
        self,
        problem_type: str,
        body: str,
        options: Optional[List[str]] = None,
        blank_count: int = 1,
    ) -> Union[str, Dict[str, str]]:
        """按题型自动选择答题策略。

        Args:
            problem_type: 题型（见模块顶部常量）。
            body: 题干文本。
            options: 选项列表（选择题）。
            blank_count: 空的数量（填空题）。

        Returns:
            选择题/判断题/简答题返回字符串；填空题返回字典。
        """
        cache_key = f"{problem_type}|{body}|{options}|{blank_count}"
        if cache_key in self._cache:
            logger.debug("命中答案缓存")
            return self._cache[cache_key]

        ptype = (problem_type or "").strip()
        if ptype in (TYPE_CHOICE, TYPE_SINGLE_CHOICE, TYPE_MULTI_CHOICE):
            multiple = ptype == TYPE_MULTI_CHOICE
            result: Union[str, Dict[str, str]] = self.answer_choice(
                body, options or [], multiple=multiple
            )
        elif ptype == TYPE_JUDGEMENT:
            result = self.answer_judgement(body)
        elif ptype == TYPE_FILL_BLANK:
            result = self.answer_fill_blank(body, blank_count)
        elif ptype == TYPE_DISCUSSION:
            result = self.answer_discussion(body)
        else:
            result = self.answer_short(body)

        self._cache[cache_key] = result
        return result

    # ==================================================================
    #  输出清洗
    # ==================================================================
    @staticmethod
    def _format_question(body: str, options: List[str]) -> str:
        lines = [body.strip()]
        if options:
            lines.append("")
            lines.append("选项：")
            lines.extend(f"  {opt}" for opt in options)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_choice(raw: str, multiple: bool) -> str:
        """将模型输出规范化为选项字母。

        优先提取“答案：”标记后的字母，避免把分析文字中的字母误当答案。
        """
        text = raw or ""

        # 1) 优先匹配“答案：X” / “答案 X” / “Answer: X” 等标记
        marker = re.search(
            r"(?:答案|正确选项|应选|answer)\s*(?:是|为|is|are|:)?\s*[:：]?\s*"
            r"([A-Za-z](?:\s*[,，、]\s*[A-Za-z])*)",
            text,
            flags=re.IGNORECASE,
        )
        segment = marker.group(1) if marker else ""

        # 2) 若无标记，取最后一行（模型通常把结论放最后）
        if not segment:
            lines = [ln for ln in text.splitlines() if ln.strip()]
            segment = lines[-1] if lines else text

        letters = re.findall(r"[A-Za-z]", segment.upper())
        if not letters:
            # 3) 兜底：全文提取
            letters = re.findall(r"[A-Za-z]", text.upper())
        if not letters:
            return "C"

        unique: List[str] = []
        for letter in letters:
            if letter not in unique:
                unique.append(letter)
        if multiple:
            return ", ".join(unique)
        return unique[0]

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_fill_blank(raw: str, blank_count: int) -> Dict[str, str]:
        """解析填空题 JSON 输出。"""
        # 尝试直接解析
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, ValueError):
            pass

        # 尝试提取 JSON 片段
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
            except (json.JSONDecodeError, ValueError):
                pass

        # 回退：按行/分隔符切分
        parts = [p.strip() for p in re.split(r"[\n,，;；]", raw) if p.strip()]
        result = {}
        for index in range(blank_count):
            result[str(index + 1)] = parts[index] if index < len(parts) else ""
        return result


class CodexClient(AIClient):
    """通过非交互式 codex exec 获取答案，无需单独的 API Key。"""

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.config.has_codex

    # ------------------------------------------------------------------
    def _command(self) -> List[str]:
        """使用参数列表执行；兼容 Windows 上 npm 安装的 Codex 包。"""
        executable = shutil.which(self.config.codex_cli)
        if not executable:
            raise AIError("未找到 Codex CLI，请安装或在 .env 设置 CODEX_CLI")
        path = Path(executable)
        if os.name == "nt" and path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            entry = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            node = shutil.which("node")
            if not node or not entry.is_file():
                raise AIError("无法启动 Codex 包，请将 CODEX_CLI 设置为 codex.exe 的完整路径")
            return [node, str(entry)]
        return [executable]

    # ------------------------------------------------------------------
    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        """停止本次调用的进程树，避免中断后留下后台答题任务。"""
        if process.poll() is None:
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True, timeout=5,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if process.poll() is None:
                process.kill()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            logger.warning("子进程输出管道尚未关闭，已停止等待")

    @staticmethod
    def _run_codex(args: List[str], *, input: str, timeout: float, **kwargs):
        """短间隔等待输出，让 Windows 有机会及时处理 Ctrl+C。"""
        process = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, **kwargs,
        )
        deadline = time.monotonic() + timeout
        pending_input = input
        try:
            while True:
                remaining = deadline - time.monotonic()
                try:
                    stdout, stderr = process.communicate(
                        input=pending_input, timeout=max(0.001, min(0.2, remaining)),
                    )
                    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired as exc:
                    # communicate 超时后可继续等待，但不能再次传入题目。
                    pending_input = None
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(
                            args, timeout, output=exc.output, stderr=exc.stderr,
                        ) from exc
        except BaseException:
            # KeyboardInterrupt 必须继续向外传播，不能作为 AI 失败重试。
            CodexClient._stop_process(process)
            raise

    # ------------------------------------------------------------------
    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        *,
        temperature: Optional[float] = None,
    ) -> str:
        """调用 Codex，读取最终回复；题目通过标准输入传入。"""
        command = self._command()
        prompt = (
            "你正在作为答题助手处理文本。只返回符合下述要求的答案。"
            "不需要操作电脑、运行命令、访问文件或调用工具。\n\n"
            f"作答要求：\n{system_prompt}\n\n题目：\n{user_prompt}"
        )

        last_error: Optional[Exception] = None
        for attempt in range(1, max_retries + 1):
            try:
                # 独立临时目录避免将项目配置、Cookie 等内容加入答题上下文。
                with tempfile.TemporaryDirectory(prefix="yuketang_codex_") as directory:
                    output = Path(directory) / "answer.txt"
                    args = command + [
                        "exec", "--skip-git-repo-check", "--ephemeral",
                        "--sandbox", "read-only", "--color", "never",
                        "--output-last-message", str(output),
                    ]
                    if self.config.codex_model:
                        args += ["--model", self.config.codex_model]
                    result = self._run_codex(
                        args + ["-"], input=prompt, cwd=directory,
                        text=True, encoding="utf-8", errors="replace",
                        timeout=self.config.codex_timeout,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    if result.returncode:
                        detail = (result.stderr or result.stdout or "无错误详情").strip()[-1500:]
                        raise AIError(
                            f"Codex 退出码 {result.returncode}：{detail}\n"
                            "请先执行 codex login status 检查登录，必要时执行 codex login。"
                        )
                    content = output.read_text(encoding="utf-8") if output.is_file() else ""
                    cleaned = self._strip_thinking(content)
                    if not cleaned:
                        raise AIError("Codex 未返回答案，请检查账号额度或增大 CODEX_TIMEOUT")
                    return cleaned
            except subprocess.TimeoutExpired as exc:
                detail = exc.stderr or b""
                if isinstance(detail, bytes):
                    detail = detail.decode("utf-8", errors="replace")
                last_error = AIError(
                    f"Codex 答题超时（{self.config.codex_timeout} 秒），可增大 CODEX_TIMEOUT"
                    + (f"\n{detail.strip()[-1500:]}" if detail else "")
                )
            except (OSError, AIError) as exc:
                last_error = exc
            logger.warning("Codex 调用失败（第 %d/%d 次）：%s", attempt, max_retries, last_error)
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 8))

        raise AIError(f"Codex 调用失败：{last_error}")


class DeepSeekClient(AIClient):
    """保留原项目的 OpenAI 兼容 API 调用，SDK 仅在此模式下加载。"""

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._client = None

    @property
    def available(self) -> bool:
        return bool(
            self.config.has_deepseek
            and self.config.deepseek_base_url
            and self.config.deepseek_model
        )

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not self.available:
            raise AIError("请配置 DEEPSEEK_API_KEY、DEEPSEEK_BASE_URL 和 DEEPSEEK_MODEL")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise AIError("缺少 openai 依赖，请执行：python -m pip install -r requirements.txt") from exc
        self._client = OpenAI(
            api_key=self.config.deepseek_api_key,
            base_url=self.config.deepseek_base_url,
            timeout=self.config.deepseek_timeout,
            max_retries=0,  # 由本模块统一重试，避免 SDK 再叠加重试。
        )
        return self._client

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        max_retries: int = 3,
        *,
        temperature: Optional[float] = None,
    ) -> str:
        client = self._get_client()
        temperature = self.config.deepseek_temperature if temperature is None else temperature
        last_error = "无错误详情"
        for attempt in range(1, max_retries + 1):
            try:
                kwargs: Dict[str, Any] = {
                    "model": self.config.deepseek_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "stream": False,
                }
                # 保留原项目对旧 reasoner 接口的兼容；思考模型可忽略温度。
                if "reasoner" not in self.config.deepseek_model.lower():
                    kwargs["temperature"] = temperature
                response = client.chat.completions.create(**kwargs)
                content = self._strip_thinking(response.choices[0].message.content or "")
                if not content:
                    raise AIError("DeepSeek 未返回答案，请检查模型配置或增大 DEEPSEEK_TIMEOUT")
                return content
            except AIError as exc:
                last_error = str(exc)
            except Exception as exc:
                # 不记录响应正文或 SDK 异常文本，避免其中带回密钥与认证头。
                status = getattr(exc, "status_code", None)
                last_error = f"{type(exc).__name__}" + (f"（HTTP {status}）" if status else "")
                if status in (400, 401, 403, 404, 422):
                    raise AIError(
                        f"DeepSeek 调用失败：{last_error}，请检查密钥、接口地址和模型配置"
                    ) from None
            logger.warning("DeepSeek 调用失败（第 %d/%d 次）：%s", attempt, max_retries, last_error)
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 8))
        raise AIError(f"DeepSeek 调用失败：{last_error}")


def create_ai_client(config: Config) -> AIClient:
    """只创建配置选中的后端，不自动回退到其他模式。"""
    if config.ai_mode == "codex":
        return CodexClient(config)
    if config.ai_mode == "deepseek":
        return DeepSeekClient(config)
    raise AIError("AI_MODE 无效，只能设置为 codex 或 deepseek")

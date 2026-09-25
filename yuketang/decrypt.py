# -*- coding: utf-8 -*-
"""雨课堂加密字体解密模块。

雨课堂部分题目文本使用自定义字体渲染，页面中表现为
``<span class="xuetangx-com-encrypted-font">`` 包裹的乱码字符。

加密原理
--------
雨课堂下发一份**子集化**的思源黑体（SourceHanSansSCVF）可变字体，
其中：

* ``cmap`` 被随机打乱（码点 → glyph 的映射是错的）；
* glyph 名称被随机化（``uniXXXX`` 名称与真实字符无关）；
* 轮廓坐标被扰动，无法直接做 MD5 / 轮廓匹配。

但**字形本身是真实的**（只是映射错了）。因此本模块采用
**渲染 + 图像匹配** 方案还原真实字符：

1. 下载加密字体；
2. 用 ``fontTools.varLib.instancer`` 实例化到 ``wght=400``
   （应用 ``gvar`` 变形，否则渲染出的是"骨架"轮廓）；
3. 将每个 glyph 渲染为二值小图；
4. 与预渲染的思源黑体候选字符做 IoU 匹配，取最高者。

参考字体：思源黑体 SC 可变字体
``https://github.com/adobe-fonts/source-han-sans/raw/release/Variable/OTF/SourceHanSansSC-VF.otf``
"""

from __future__ import annotations

import io
import os
import re
from typing import Dict, Optional

from .logger import get_logger

logger = get_logger("decrypt")

# 加密字体 span 的正则
ENCRYPTED_SPAN_RE = re.compile(
    r'<span class="xuetangx-com-encrypted-font">(.*?)</span>', re.DOTALL
)

# 字体 URL 提取正则
FONT_URL_RE = re.compile(r"url\(['\"]?(https?://[^'\")]+\.(?:ttf|woff2?))['\"]?\)")

# 参考字体下载地址（思源黑体 SC 可变字体）
REFERENCE_FONT_URL = (
    "https://github.com/adobe-fonts/source-han-sans/raw/release/"
    "Variable/OTF/SourceHanSansSC-VF.otf"
)

# 渲染参数
_RENDER_SIZE = 96
_MATCH_SIZE = 48
_WEIGHT = 400


class FontDecryptor:
    """基于渲染 + 图像匹配的字体解密器。

    使用方式::

        decryptor = FontDecryptor(cache_dir="data/font_cache")
        decryptor.load_from_url(font_url)
        plain = decryptor.decrypt(html_text)
    """

    def __init__(
        self,
        mapping: Optional[Dict[str, str]] = None,
        cache_dir: Optional[str] = None,
    ) -> None:
        # 兼容旧接口：glyph_md5 -> 真实字符（不再使用，保留占位）
        self.mapping: Dict[str, str] = dict(mapping or {})
        # 原始字符 -> 真实字符
        self.char_map: Dict[str, str] = {}
        self._loaded = False
        self.cache_dir = cache_dir
        # 参考字体路径（思源黑体 wght=400 实例）
        self._ref_font_path: Optional[str] = None
        # 预渲染的候选字符图像缓存 {char: np.ndarray}
        self._ref_cache: Optional[Dict[str, object]] = None

    # ------------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self._loaded

    # ------------------------------------------------------------------
    def load_from_css(self, css_text: str) -> bool:
        """从 CSS 文本中提取字体 URL 并加载。"""
        match = FONT_URL_RE.search(css_text or "")
        if not match:
            logger.debug("CSS 中未找到字体 URL")
            return False
        return self.load_from_url(match.group(1))

    # ------------------------------------------------------------------
    def load_from_url(self, url: str) -> bool:
        """下载字体文件并构建字符映射。"""
        try:
            import requests
            from fontTools.ttLib import TTFont
        except ImportError as exc:
            logger.warning("缺少 fonttools/requests 依赖：%s", exc)
            return False

        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
            font = TTFont(io.BytesIO(resp.content))
        except Exception as exc:  # pragma: no cover
            logger.warning("加载字体失败：%s", exc)
            return False

        return self._build_char_map(font)

    # ------------------------------------------------------------------
    def load_from_file(self, path: str) -> bool:
        """从本地字体文件构建字符映射。"""
        try:
            from fontTools.ttLib import TTFont

            font = TTFont(path)
        except Exception as exc:  # pragma: no cover
            logger.warning("加载字体文件失败：%s", exc)
            return False
        return self._build_char_map(font)

    # ------------------------------------------------------------------
    def _build_char_map(self, font) -> bool:
        """渲染加密字体 glyph，与参考字体匹配，构建 原始字符 -> 真实字符 映射。"""
        try:
            from fontTools.varLib import instancer
            from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        except ImportError as exc:
            logger.warning("缺少 fonttools/Pillow 依赖：%s", exc)
            return False

        # 1. 实例化到 wght=400（应用 gvar 变形）
        try:
            inst = instancer.instantiateVariableFont(font, {"wght": _WEIGHT})
        except Exception as exc:  # pragma: no cover
            logger.warning("实例化可变字体失败，使用原始字体：%s", exc)
            inst = font

        # 2. 保存实例化字体到临时文件（Pillow 需要文件路径）
        import tempfile

        tmp = tempfile.NamedTemporaryFile(suffix=".ttf", delete=False)
        tmp.close()
        try:
            inst.save(tmp.name)
        except Exception as exc:  # pragma: no cover
            logger.warning("保存实例化字体失败：%s", exc)
            return False

        # 3. 准备参考字体
        ref_path = self._ensure_reference_font()
        if not ref_path:
            logger.warning("无法获取参考字体，解密不可用")
            return False

        # 4. 预渲染参考字体候选
        ref_cache = self._get_ref_cache(ref_path)
        if not ref_cache:
            logger.warning("参考字体预渲染失败")
            return False

        # 5. 逐个 glyph 匹配
        cmap = inst.getBestCmap()
        matched = 0
        # 将参考缓存堆叠为矩阵，便于向量化匹配
        import numpy as np

        ref_chars = list(ref_cache.keys())
        ref_matrix = np.stack([ref_cache[c] for c in ref_chars])  # (N, 48, 48)
        ref_flat = ref_matrix.reshape(len(ref_chars), -1)  # (N, 2304)

        for code in cmap.keys():
            ch = chr(code)
            enc_img = self._render_char(tmp.name, ch)
            if enc_img is None:
                continue
            real = self._match_fast(enc_img, ref_chars, ref_flat)
            if real:
                self.char_map[ch] = real
                matched += 1

        try:
            os.unlink(tmp.name)
        except OSError:
            pass

        self._loaded = matched > 0
        logger.debug("字体解密映射构建完成，共 %d 个字符", matched)
        return self._loaded

    # ------------------------------------------------------------------
    def _ensure_reference_font(self) -> Optional[str]:
        """确保参考字体（思源黑体 wght=400 实例）可用，返回路径。"""
        if self._ref_font_path and os.path.exists(self._ref_font_path):
            return self._ref_font_path

        cache_dir = self.cache_dir or os.path.join(
            os.path.expanduser("~"), ".cache", "yuketang"
        )
        os.makedirs(cache_dir, exist_ok=True)
        ref_path = os.path.join(cache_dir, "SourceHanSansSC-wght400.ttf")

        if os.path.exists(ref_path):
            self._ref_font_path = ref_path
            return ref_path

        # 下载可变字体并实例化
        try:
            import requests
            from fontTools.ttLib import TTFont
            from fontTools.varLib import instancer

            logger.info("首次运行：下载参考字体（思源黑体，约 30MB）…")
            resp = requests.get(REFERENCE_FONT_URL, timeout=120)
            resp.raise_for_status()
            vf = TTFont(io.BytesIO(resp.content))
            inst = instancer.instantiateVariableFont(vf, {"wght": _WEIGHT})
            inst.save(ref_path)
            self._ref_font_path = ref_path
            logger.info("参考字体已缓存：%s", ref_path)
            return ref_path
        except Exception as exc:  # pragma: no cover
            logger.warning("下载参考字体失败：%s", exc)
            return None

    # ------------------------------------------------------------------
    def _get_ref_cache(self, ref_path: str) -> Optional[Dict[str, object]]:
        """预渲染参考字体的常用汉字，返回 {char: 二值图像}。

        结果会缓存到磁盘（``<cache_dir>/ref_cache.npz``），避免每次重复渲染。
        """
        if self._ref_cache is not None:
            return self._ref_cache

        import numpy as np

        cache_dir = self.cache_dir or os.path.join(
            os.path.expanduser("~"), ".cache", "yuketang"
        )
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, "ref_cache.npz")

        # 尝试从磁盘加载
        if os.path.exists(cache_file):
            try:
                loaded = np.load(cache_file, allow_pickle=False)
                cache = {k: loaded[k] for k in loaded.files}
                self._ref_cache = cache
                logger.debug("参考字体缓存已加载，共 %d 个候选", len(cache))
                return cache
            except Exception as exc:  # pragma: no cover
                logger.debug("加载参考缓存失败，重新渲染：%s", exc)

        # 重新渲染
        cache: Dict[str, object] = {}
        for code in range(0x4E00, 0x9FA6):
            img = self._render_char(ref_path, chr(code))
            if img is not None:
                cache[chr(code)] = img
        self._ref_cache = cache

        # 保存到磁盘
        try:
            np.savez_compressed(cache_file, **cache)
            logger.debug("参考字体缓存已保存：%s", cache_file)
        except Exception as exc:  # pragma: no cover
            logger.debug("保存参考缓存失败：%s", exc)

        logger.debug("参考字体预渲染完成，共 %d 个候选", len(cache))
        return cache

    # ------------------------------------------------------------------
    @staticmethod
    def _render_char(font_path: str, ch: str) -> Optional[object]:
        """将单个字符渲染为归一化二值图像（48x48）。"""
        try:
            from PIL import Image, ImageDraw, ImageFont
            import numpy as np
        except ImportError:  # pragma: no cover
            return None

        size = _RENDER_SIZE
        img = Image.new("L", (size, size), 255)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(font_path, int(size * 0.8))
        except Exception:  # pragma: no cover
            return None

        bbox = draw.textbbox((0, 0), ch, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w <= 0 or h <= 0:
            return None
        draw.text(
            ((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]),
            ch,
            font=font,
            fill=0,
        )
        arr = np.array(img)
        ys, xs = np.where(arr < 128)
        if len(xs) == 0:
            return None
        crop = arr[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        im = Image.fromarray(crop).resize((_MATCH_SIZE, _MATCH_SIZE), Image.LANCZOS)
        return (np.array(im) < 128).astype(np.float32)

    # ------------------------------------------------------------------
    @staticmethod
    def _match(enc_img: object, ref_cache: Dict[str, object]) -> Optional[str]:
        """在参考缓存中查找 IoU 最高的字符（逐项比较，较慢）。"""
        best_char: Optional[str] = None
        best_iou = 0.0
        for ch, ref_img in ref_cache.items():
            inter = float((enc_img * ref_img).sum())
            union = float(((enc_img + ref_img) > 0).sum())
            if union <= 0:
                continue
            iou = inter / union
            if iou > best_iou:
                best_iou = iou
                best_char = ch
        # 阈值：低于 0.5 认为不可信
        if best_iou < 0.5:
            return None
        return best_char

    # ------------------------------------------------------------------
    @staticmethod
    def _match_fast(
        enc_img: object, ref_chars: list, ref_flat: object
    ) -> Optional[str]:
        """向量化 IoU 匹配，返回最相似的字符。"""
        import numpy as np

        enc_flat = enc_img.reshape(-1)  # (2304,)
        inter = ref_flat @ enc_flat  # (N,)
        union = ref_flat.sum(axis=1) + enc_flat.sum() - inter  # (N,)
        union[union <= 0] = 1.0
        ious = inter / union
        idx = int(np.argmax(ious))
        if ious[idx] < 0.5:
            return None
        return ref_chars[idx]

    # ------------------------------------------------------------------
    def decrypt(self, text: str) -> str:
        """解密文本中的加密字体字符。"""
        if not text or not self.char_map:
            return text
        return "".join(self.char_map.get(ch, ch) for ch in text)

    # ------------------------------------------------------------------
    def decrypt_html(self, html: str) -> str:
        """解密 HTML 中 ``xuetangx-com-encrypted-font`` span 内的文本。"""
        if not html:
            return html

        def _replace(match: "re.Match[str]") -> str:
            inner = match.group(1)
            return self.decrypt(inner)

        return ENCRYPTED_SPAN_RE.sub(_replace, html)


# ----------------------------------------------------------------------
def load_mapping_from_file(path: str) -> Dict[str, str]:
    """从 JSON 文件加载映射表（兼容旧接口，新方案不再依赖）。"""
    import json

    if not os.path.exists(path):
        logger.debug("映射文件不存在：%s", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception as exc:  # pragma: no cover
        logger.warning("加载映射文件失败：%s", exc)
    return {}

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长江雨课堂自动刷课 + DeepSeek 智能答题工具 —— 程序入口。

用法::

    python main.py login          # 扫码登录
    python main.py courses        # 查看课程
    python main.py video          # 刷视频
    python main.py all            # 一键完成全部
"""

import sys

from yuketang.cli import main

if __name__ == "__main__":
    sys.exit(main())

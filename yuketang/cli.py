# -*- coding: utf-8 -*-
"""命令行入口。

提供子命令：login / courses / video / richtext / discussion / homework / all。
所有操作均为非交互式，配置来自 ``.env``。
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .ai import DeepSeekClient
from .config import Config
from .course import Course, CourseAPI
from .homework import HomeworkWorker
from .logger import banner, fail, get_logger, ok, setup_logger, step, warn
from .login import LoginManager
from .richtext import DiscussionWorker, RichtextWorker
from .session import YuketangSession
from .video import VideoWatcher

logger = get_logger("cli")


# ======================================================================
#  辅助
# ======================================================================
def _build_session(config: Config) -> YuketangSession:
    """根据配置构造已登录的会话。"""
    cookies = config.cookies or config.build_cookies()
    return YuketangSession(
        domain=config.domain,
        cookies=cookies,
        university_id=config.university_id,
        csrf_token=config.csrf_token,
        max_retries=config.max_retries,
        user_id=config.user_id,
    )


def _ensure_login(config: Config, force_qrcode: bool = False) -> Optional[YuketangSession]:
    """确保已登录，必要时触发扫码。"""
    manager = LoginManager(config)
    session = manager.login(force_qrcode=force_qrcode)
    if session is None:
        fail("登录失败，请检查网络或重新扫码")
        return None
    return session


def _select_courses(
    api: CourseAPI, config: Config, keyword: Optional[str] = None
) -> List[Course]:
    """获取课程列表，可按关键字过滤。"""
    courses = api.list_courses()
    if not courses:
        warn("未获取到任何课程")
        return []
    if keyword:
        courses = [c for c in courses if keyword in c.course_name]
        if not courses:
            warn(f"没有匹配关键字「{keyword}」的课程")
    return courses


# ======================================================================
#  子命令实现
# ======================================================================
def cmd_login(config: Config, args: argparse.Namespace) -> int:
    banner("扫码登录")
    session = _ensure_login(config, force_qrcode=args.qrcode)
    if session is None:
        return 1
    ok("登录成功，凭证已保存到 .env")
    return 0


def cmd_courses(config: Config, args: argparse.Namespace) -> int:
    banner("课程列表")
    session = _ensure_login(config)
    if session is None:
        return 1
    api = CourseAPI(session, config.university_id)
    courses = api.list_courses()
    if not courses:
        warn("没有进行中的课程")
        return 1
    for index, course in enumerate(courses, 1):
        print(f"  {index:>2}. {course.course_name}")
        print(f"      classroom_id={course.classroom_id}  sku_id={course.sku_id}")
    return 0


def cmd_video(config: Config, args: argparse.Namespace) -> int:
    banner("视频刷课")
    session = _ensure_login(config)
    if session is None:
        return 1
    api = CourseAPI(session, config.university_id)
    courses = _select_courses(api, config, args.course)
    if not courses:
        return 1

    watcher = VideoWatcher(session, config)
    total = {"total": 0, "success": 0, "skipped": 0, "failed": 0}
    for course in courses:
        logger.info("══ 课程：%s", course.course_name)
        stats = watcher.watch_course(
            course,
            speed=args.speed,
            interval=args.interval,
            skip_completed=not args.no_skip,
            max_workers=args.workers,
        )
        for key in total:
            total[key] += stats.get(key, 0)

    logger.info(
        "全部完成：成功 %d / 跳过 %d / 失败 %d（共 %d）",
        total["success"], total["skipped"], total["failed"], total["total"],
    )
    return 0


def cmd_richtext(config: Config, args: argparse.Namespace) -> int:
    banner("图文打卡")
    session = _ensure_login(config)
    if session is None:
        return 1
    api = CourseAPI(session, config.university_id)
    courses = _select_courses(api, config, args.course)
    if not courses:
        return 1

    worker = RichtextWorker(session, config)
    for course in courses:
        logger.info("══ 课程：%s", course.course_name)
        worker.finish_course(course)
    return 0


def cmd_discussion(config: Config, args: argparse.Namespace) -> int:
    banner("讨论区发帖")
    session = _ensure_login(config)
    if session is None:
        return 1
    api = CourseAPI(session, config.university_id)
    courses = _select_courses(api, config, args.course)
    if not courses:
        return 1

    ai = DeepSeekClient(config)
    if not ai.available:
        warn("未配置 DeepSeek，讨论内容将无法生成")
    worker = DiscussionWorker(session, config, ai)
    for course in courses:
        logger.info("══ 课程：%s", course.course_name)
        worker.post_course(course)
    return 0


def cmd_homework(config: Config, args: argparse.Namespace) -> int:
    banner("作业 / 考试自动答题")
    session = _ensure_login(config)
    if session is None:
        return 1
    api = CourseAPI(session, config.university_id)
    courses = _select_courses(api, config, args.course)
    if not courses:
        return 1

    ai = DeepSeekClient(config)
    if not ai.available:
        fail("未配置 DEEPSEEK_API_KEY，无法自动答题")
        return 1

    worker = HomeworkWorker(session, config, ai)
    for course in courses:
        logger.info("══ 课程：%s", course.course_name)
        worker.solve_course(course, include_exam=args.exam)
    return 0


def cmd_all(config: Config, args: argparse.Namespace) -> int:
    banner("一键刷课（视频 + 图文 + 讨论 + 作业）")
    session = _ensure_login(config)
    if session is None:
        return 1
    api = CourseAPI(session, config.university_id)
    courses = _select_courses(api, config, args.course)
    if not courses:
        return 1

    ai = DeepSeekClient(config)

    for course in courses:
        logger.info("════════ 课程：%s ════════", course.course_name)

        # 1. 视频
        if config.auto_richtext or True:  # 视频默认开启
            step("视频刷课")
            VideoWatcher(session, config).watch_course(
                course, speed=args.speed, interval=args.interval
            )

        # 2. 图文
        if config.auto_richtext:
            step("图文打卡")
            RichtextWorker(session, config).finish_course(course)

        # 3. 讨论
        if config.auto_discussion:
            step("讨论发帖")
            DiscussionWorker(session, config, ai).post_course(course)

        # 4. 作业 / 考试
        if config.auto_homework or config.auto_exam:
            step("作业答题")
            HomeworkWorker(session, config, ai).solve_course(
                course, include_exam=config.auto_exam
            )

    ok("全部任务完成")
    return 0


# ======================================================================
#  参数解析
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yuketang",
        description="长江雨课堂自动刷课 + DeepSeek 智能答题工具",
    )
    parser.add_argument("--debug", action="store_true", help="开启调试日志")
    parser.add_argument("--quiet", action="store_true", help="仅输出警告与错误")

    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    # login
    p_login = sub.add_parser("login", help="扫码登录并保存凭证")
    p_login.add_argument("--qrcode", action="store_true", help="强制使用扫码登录")
    p_login.set_defaults(func=cmd_login)

    # courses
    p_courses = sub.add_parser("courses", help="列出所有课程")
    p_courses.set_defaults(func=cmd_courses)

    # video
    p_video = sub.add_parser("video", help="自动刷视频")
    p_video.add_argument("-c", "--course", help="课程名称关键字过滤")
    p_video.add_argument("-s", "--speed", type=float, help="播放倍速")
    p_video.add_argument("-i", "--interval", type=int, help="心跳间隔（秒）")
    p_video.add_argument("-w", "--workers", type=int, help="并发数")
    p_video.add_argument("--no-skip", action="store_true", help="不跳过已完成视频")
    p_video.set_defaults(func=cmd_video)

    # richtext
    p_rich = sub.add_parser("richtext", help="自动完成图文打卡")
    p_rich.add_argument("-c", "--course", help="课程名称关键字过滤")
    p_rich.set_defaults(func=cmd_richtext)

    # discussion
    p_disc = sub.add_parser("discussion", help="自动完成讨论发帖")
    p_disc.add_argument("-c", "--course", help="课程名称关键字过滤")
    p_disc.set_defaults(func=cmd_discussion)

    # homework
    p_hw = sub.add_parser("homework", help="自动完成作业 / 考试")
    p_hw.add_argument("-c", "--course", help="课程名称关键字过滤")
    p_hw.add_argument("--exam", action="store_true", help="同时处理考试")
    p_hw.set_defaults(func=cmd_homework)

    # all
    p_all = sub.add_parser("all", help="一键完成全部任务")
    p_all.add_argument("-c", "--course", help="课程名称关键字过滤")
    p_all.add_argument("-s", "--speed", type=float, help="播放倍速")
    p_all.add_argument("-i", "--interval", type=int, help="心跳间隔（秒）")
    p_all.set_defaults(func=cmd_all)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    config = Config.load()
    if args.debug:
        config.debug = True
    setup_logger(log_file=config.log_file, debug=config.debug, quiet=args.quiet)

    # 配置校验提示
    problems = config.validate()
    for problem in problems:
        warn(problem)

    try:
        return args.func(config, args)
    except KeyboardInterrupt:
        warn("用户中断")
        return 130
    except Exception as exc:  # noqa: BLE001
        logger.exception("执行出错：%s", exc)
        fail(f"执行出错：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

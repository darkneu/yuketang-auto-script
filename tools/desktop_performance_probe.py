"""Offline controlled-delay probe; never reads credentials or accesses a service."""
import json
import logging
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtWidgets import QApplication, QLabel
from yuketang.config import Config
from yuketang.course import Course, Leaf
from yuketang.desktop_backend import DesktopService
from yuketang.desktop_runtime import _worker
from yuketang.homework import HomeworkWorker
from yuketang.richtext import RichtextWorker
from yuketang.session import YuketangSession

logging.disable(logging.CRITICAL)

def measure():
    result = {}
    session = YuketangSession('example.invalid')
    cfg = Config(auto_video=False, auto_richtext=False, auto_homework=True, deepseek_api_key='offline')
    course = Course('offline', 1, '', 1, 1)
    service = DesktopService(cfg)
    service.session, service.course = session, course
    session.observer = lambda *_: None
    service.leaves = [Leaf(i, 'completed', 6) for i in range(3)]
    counts = {'requests': 0, 'font_loads': 0, 'parses': 0}
    def get(url, **kwargs):
        counts['requests'] += 1
        time.sleep(0.01)
        if 'leaf_info' in url:
            return {'success': True, 'data': {'sku_id': 1, 'content_info': {'leaf_type_id': 1}}}
        return {'success': True, 'data': {'font': 'https://example.invalid/font.ttf', 'problems': [
            {'problem_id': i, 'content': {'Type': 'ShortAnswer', 'Body': '<p>completed</p>'},
             'user': {'my_count': 1, 'count': 1}} for i in range(20)]}}
    def font(*_):
        counts['font_loads'] += 1
        time.sleep(0.2)
        return True
    parse = HomeworkWorker._parse_problem
    def parsed(self, problem):
        counts['parses'] += 1
        return parse(self, problem)
    with patch.object(session, 'get_json', side_effect=get), patch('yuketang.homework.FontDecryptor.load_from_url', side_effect=font), patch.object(HomeworkWorker, '_parse_problem', parsed), patch('yuketang.desktop_backend.create_ai_client'):
        start = time.perf_counter()
        service.run_course()
        result['completed_homework'] = dict(counts, elapsed_ms=round((time.perf_counter()-start)*1000, 2))
    urls = []
    def rich_get(url, **kwargs):
        urls.append('leaf_info' if 'leaf_info' in url else 'article_status')
        time.sleep(0.01)
        return {'success': True, 'data': {'finish': 1}}
    with patch.object(session, 'get_json', side_effect=rich_get):
        RichtextWorker(session, cfg).finish_one(course, Leaf(4, 'completed', 3))
    result['completed_richtext_requests'] = urls
    produced = []
    class SlowPipe:
        def send(self, event):
            time.sleep(0.01)
        def close(self):
            pass
    def run(self):
        start = time.perf_counter()
        for i in range(20):
            self.emit('task', (i, {'state': '处理中'}))
        produced.append(round((time.perf_counter()-start)*1000, 2))
    with patch.object(DesktopService, 'run_course', run):
        _worker('course', cfg, SlowPipe(), session.stop_event,
                {'session_data': {'domain': 'example.invalid'}, 'course': course, 'leaves': []})
    result['producer_ms_with_20_slow_pipe_events'] = produced[0]
    from yuketang.desktop import MainWindow, configure_app
    app = QApplication.instance() or QApplication([])
    configure_app(app)
    window = MainWindow(demo=True)
    window.service.config.auto_homework = True
    window.start()
    window.demo_timer.stop()
    writes = [0]
    original = QLabel.setText
    def set_text(label, value):
        writes[0] += 1
        original(label, value)
    with patch.object(QLabel, 'setText', set_text):
        start = time.perf_counter()
        for i in range(80):
            for state in ('生成答案中', '提交中', '已提交'):
                window.on_event('task', (6, {'state': '处理中', 'problem': {'id': i, 'type': 'ShortAnswer',
                    'body': 'offline question', 'state': state, 'answer': 'offline answer'}}))
        elapsed = time.perf_counter()-start
    result['history_80_questions_240_events'] = {'label_writes': writes[0], 'elapsed_ms': round(elapsed*1000, 2)}
    window.stop()
    window.close()
    session.session.close()
    return result

if __name__ == '__main__':
    print(json.dumps(measure(), ensure_ascii=False, indent=2))


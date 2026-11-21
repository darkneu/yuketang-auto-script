"""Render native windows with local fixtures; never loads .env or calls a network."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PySide6.QtWidgets import QApplication
from yuketang.config import Config
from yuketang.desktop import MainWindow, SettingsDialog, LogoutDialog, configure_app, app_icon
from yuketang.desktop_backend import DesktopService

output = Path(__file__).resolve().parent.parent / ".build_tmp" / "desktop-preview"
output.mkdir(parents=True, exist_ok=True)
app = QApplication([])
configure_app(app)
def settle():
    for _ in range(8):
        app.processEvents()
def capture(widget, name):
    settle()
    widget.grab().save(str(output / name))
window = MainWindow(demo=True)
window.show()
window.course_list.item(0).setText("2026秋 设计思维与创新课程（长标题排版检查）01")
capture(window, "structure.png")
for key in ("auto_discussion", "auto_homework", "auto_exam"):
    setattr(window.service.config, key, True)
window.start()
window.demo_timer.stop()
for _ in range(2):
    window.demo_tick()
capture(window, "course.png")
window.toggle_run_view()
capture(window, "progress.png")
window.toggle_run_view()
window.run_panel.scroll.verticalScrollBar().setValue(window.run_panel.scroll.verticalScrollBar().maximum())
capture(window, "homework.png")
window.stop()
logout = LogoutDialog(window)
logout.show()
capture(logout, "logout.png")
logout.close()
dialog = SettingsDialog(Config(), window, demo=True)
dialog.show()
capture(dialog, "settings.png")
dialog.fields["ai_mode"].setCurrentIndex(1)
capture(dialog, "settings-codex.png")
for index, name in ((1, "settings-tasks.png"), (2, "settings-connection.png")):
    dialog.tabs.setCurrentIndex(index)
    capture(dialog, name)
dialog.tabs.setCurrentIndex(0)
dialog.fields["ai_mode"].showPopup()
capture(dialog.fields["ai_mode"].view().window(), "settings-dropdown.png")
dialog.fields["ai_mode"].hidePopup()
dialog.close()
window.close()
login = MainWindow(service=DesktopService(Config()))
login.show()
capture(login, "login.png")
login.close()
app_icon().pixmap(128, 128).save(str(Path(__file__).resolve().parent.parent / "assets" / "app.ico"))
print(output)

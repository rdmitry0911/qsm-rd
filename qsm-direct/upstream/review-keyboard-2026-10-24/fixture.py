import os,json,time,fcntl
from pathlib import Path
os.environ.update(QT_QPA_PLATFORM='wayland', XDG_RUNTIME_DIR='/run/qsm-display-1', WAYLAND_DISPLAY='wayland-1')
os.setgroups([]);os.setgid(999);os.setuid(999)
from PyQt5 import QtWidgets,QtCore
app=QtWidgets.QApplication([])
w=QtWidgets.QWidget();w.setWindowTitle('QSM isolated keyboard timing fixture')
label=QtWidgets.QLabel('QSM keyboard test — CT108 lab only',w);label.setGeometry(80,50,1000,70)
edit=QtWidgets.QLineEdit(w);edit.setGeometry(80,160,1000,60);edit.setStyleSheet('font-size:28px;')
root=Path('/run/qsm-keyboard24');root.mkdir(exist_ok=True)
lock=(root/'fixture.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
def sample():
 if (root/'reset').exists():
  (root/'reset').unlink();edit.clear();edit.setFocus()
 state={'length':len(edit.text()),'uppercase':edit.text().isupper(),'selected':len(edit.selectedText()),'focus':edit.hasFocus(),'width':w.width(),'height':w.height(),'time':time.time()}
 (root/'result.new').write_text(json.dumps(state));(root/'result.new').replace(root/'result.json')
timer=QtCore.QTimer();timer.timeout.connect(sample);timer.start(40)
w.showFullScreen();w.activateWindow();edit.setFocus();app.exec_()

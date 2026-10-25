import os,json,fcntl,time
from pathlib import Path
os.environ.update(QT_QPA_PLATFORM='wayland',XDG_RUNTIME_DIR='/run/qsm-display-1',WAYLAND_DISPLAY='wayland-1')
os.setgroups([]);os.setgid(999);os.setuid(999)
from PyQt5 import QtWidgets,QtCore
root=Path('/run/qsm-keycapture25');lock=(root/'fixture.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
app=QtWidgets.QApplication([]);events=[]
class Surface(QtWidgets.QWidget):
 def keyPressEvent(self,e):events.append(['press',e.key(),int(e.modifiers())]);e.accept()
 def keyReleaseEvent(self,e):events.append(['release',e.key(),int(e.modifiers())]);e.accept()
w=Surface();w.setWindowTitle('QSM isolated fullscreen keyboard fixture');w.setFocusPolicy(QtCore.Qt.StrongFocus)
def sample():
 if (root/'reset').exists():(root/'reset').unlink();events.clear();w.setFocus()
 (root/'result.new').write_text(json.dumps({'events':events,'focus':w.hasFocus(),'width':w.width(),'height':w.height()}));(root/'result.new').replace(root/'result.json')
timer=QtCore.QTimer();timer.timeout.connect(sample);timer.start(30)
w.showFullScreen();w.activateWindow();w.setFocus();app.exec_()

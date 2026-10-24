import os,json,time,fcntl
from pathlib import Path
os.environ.update(QT_QPA_PLATFORM='wayland',XDG_RUNTIME_DIR='/run/qsm-display-1',WAYLAND_DISPLAY='wayland-1')
os.setgroups([]);os.setgid(999);os.setuid(999)
from PyQt5 import QtWidgets,QtCore
root=Path('/run/qsm-mouse24');lock=(root/'fixture.lock').open('w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
app=QtWidgets.QApplication([]);w=QtWidgets.QWidget();w.setWindowTitle('QSM isolated mouse timing fixture')
button=QtWidgets.QPushButton('Auto repeat test',w);button.setGeometry(80,80,250,120);button.setAutoRepeat(True);button.setAutoRepeatDelay(300);button.setAutoRepeatInterval(80)
state={};down={}
def reset():
 state.clear();state.update(press=[],release=[],drag=[],double=0,clicks=0,wheel=[],holds=[]);down.clear()
def clicked():state['clicks']+=1
button.clicked.connect(clicked)
reset()
class Observer(QtCore.QObject):
 def eventFilter(self,obj,e):
  if obj not in (w,button):return False
  t=e.type()
  if t in (QtCore.QEvent.MouseButtonPress,QtCore.QEvent.MouseButtonRelease,QtCore.QEvent.MouseButtonDblClick):
   xy=w.mapFromGlobal(e.globalPos());point=[xy.x(),xy.y(),int(e.button())]
   if t==QtCore.QEvent.MouseButtonRelease:
    state['release'].append(point)
    if int(e.button()) in down:state['holds'].append(round((time.monotonic()-down.pop(int(e.button())))*1000,2))
   else:
    state['press'].append(point);down[int(e.button())]=time.monotonic()
    if t==QtCore.QEvent.MouseButtonDblClick:state['double']+=1
  elif t==QtCore.QEvent.MouseMove and e.buttons():
   xy=w.mapFromGlobal(e.globalPos());state['drag'].append([xy.x(),xy.y(),int(e.buttons())])
  elif t==QtCore.QEvent.Wheel:
   state['wheel'].append([e.angleDelta().x(),e.angleDelta().y()]);e.accept();return True
  return False
observer=Observer();app.installEventFilter(observer)
def sample():
 if (root/'reset').exists():(root/'reset').unlink();reset()
 (root/'result.new').write_text(json.dumps(state|{'width':w.width(),'height':w.height()}));(root/'result.new').replace(root/'result.json')
timer=QtCore.QTimer();timer.timeout.connect(sample);timer.start(30)
w.showFullScreen();w.activateWindow();app.exec_()

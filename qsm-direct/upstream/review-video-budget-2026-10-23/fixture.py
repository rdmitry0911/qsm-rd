import os,pwd
from pathlib import Path
from PyQt5.QtWidgets import QApplication,QWidget
from PyQt5.QtGui import QImage,QPainter,QColor
from PyQt5.QtCore import QTimer
user=pwd.getpwnam('qsm-display');runtime=Path('/run/qsm-display-1')
sock=next(p for p in runtime.glob('wayland-*') if p.is_socket())
os.environ.update(QT_QPA_PLATFORM='wayland',WAYLAND_DISPLAY=sock.name,XDG_RUNTIME_DIR=str(runtime))
os.initgroups(user.pw_name,user.pw_gid);os.setgid(user.pw_gid);os.setuid(user.pw_uid)
raw=Path('/opt/qsm-bands23/fixture.bgra').read_bytes();image=QImage(raw,2560,1440,QImage.Format_ARGB32)
app=QApplication([])
class Fixture(QWidget):
 def __init__(self):
  super().__init__();self.n=0;self.timer=QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(33)
 def tick(self):self.n+=1;self.update()
 def paintEvent(self,e):
  p=QPainter(self);p.drawImage(0,0,image)
  if Path('/run/qsm-bands23-motion').exists():p.fillRect(1600+(self.n*7)%220,100+(self.n*5)%700,100,100,QColor('#ff5500'))
w=Fixture();w.showFullScreen();app.exec_()

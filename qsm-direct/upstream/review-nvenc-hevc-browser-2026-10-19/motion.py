import os
from PyQt5.QtGui import QPixmap
from PyQt5.QtCore import Qt,QTimer
from PyQt5.QtGui import QColor,QPainter,QLinearGradient
from PyQt5.QtWidgets import QApplication,QWidget
import math
class Motion(QWidget):
 def __init__(self):
  super().__init__();self.n=0;self.setWindowTitle('QSM continuous video transport fixture')
  self.timer=QTimer(self);self.timer.timeout.connect(self.tick);self.timer.start(16)
 def tick(self):
  if not os.path.exists("/run/qsm-nvenc-browser24-static"):self.n+=1
  self.update()
 def paintEvent(self,e):
  p=QPainter(self);w,h=self.width(),self.height()
  if os.path.exists("/run/qsm-nvenc-browser24-static"):
   p.drawPixmap(self.rect(),QPixmap("/opt/qsm-nvenc-browser24/static.png"));return
  g=QLinearGradient(0,0,w,h);g.setColorAt(0,QColor.fromHsv((self.n*2)%360,170,180));g.setColorAt(1,QColor.fromHsv((self.n*2+90)%360,180,240));p.fillRect(self.rect(),g)
  for i in range(90):
   x=int((i*133+self.n*(i%7+1)*3)%(w+100))-50;y=int((i*37+self.n*(i%3+1))%h)
   p.fillRect(x,y,90,45,QColor.fromHsv((i*17+self.n)%360,210,240))
  p.setPen(Qt.white);p.drawText(30,40,'QSM continuous moving picture — frame '+str(self.n))
app=QApplication([]);w=Motion();w.showFullScreen();app.exec_()

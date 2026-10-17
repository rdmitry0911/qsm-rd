#!/usr/bin/env python3
"""Disposable CT108 fixture: real Qt/Xwayland hover paint and window movement."""
from PyQt5.QtCore import Qt, QTimer
from pathlib import Path
from PyQt5.QtGui import QColor, QPainter, QCursor
from PyQt5.QtWidgets import QApplication, QWidget

class Fixture(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint)
        self.setWindowTitle('QSM repaint latency fixture')
        self.setGeometry(100, 100, 640, 400)
        self.setMouseTracking(True)
        self.drag = None
        self.right = False
        self.counter = 0
        self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(16)
    def tick(self):
        if Path('/tmp/qsm-repaint-animate').exists():
            self.counter = (self.counter + 1) % 32
            self.update()
    def paintEvent(self, event):
        p=QPainter(self)
        p.fillRect(self.rect(), QColor('#ff00ff' if self.right else '#0066ff'))
        gray=self.counter*8
        p.fillRect(16,60,64,64,QColor(gray,gray,gray))
        p.fillRect(0,0,self.width(),40,QColor('#ffff00'))
        p.setPen(Qt.black)
        p.drawText(16,25,'Drag this real Qt window')
    def mouseMoveEvent(self, e):
        if self.drag is not None:
            self.move(e.globalPos()-self.drag)
        else:
            self.right=e.pos().x()>=320
            self.setCursor(QCursor(Qt.BlankCursor if e.pos().y()>300 else (Qt.IBeamCursor if self.right else Qt.ArrowCursor)))
            self.update()
    def mousePressEvent(self,e):
        if e.button()==Qt.LeftButton and e.pos().y()<40:
            self.drag=e.globalPos()-self.pos()
    def mouseReleaseEvent(self,e):
        self.drag=None

app=QApplication([])
w=Fixture();w.show();app.exec_()

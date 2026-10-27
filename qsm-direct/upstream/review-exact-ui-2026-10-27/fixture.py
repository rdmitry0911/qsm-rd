"""Owned mixed-video/text and real overlapping-window fixture; no personal data."""
import json,time,sys,os,glob,subprocess
from pathlib import Path
from PyQt5 import QtCore,QtGui,QtWidgets
ROOT=Path('/run/qsm-exact-ui-lab');ROOT.mkdir(exist_ok=True)
app=QtWidgets.QApplication([]);app.setDesktopFileName('qsm-exact-ui-lab')
COLORS=[(240,40,40),(40,240,40),(40,40,240),(240,240,40),(240,40,240),(40,240,240),(240,240,240),(40,40,40)]
state={'characters':0,'video_frames':0,'overlap':False,'events':[]}
class Base(QtWidgets.QWidget):
 def __init__(self):
  super().__init__();self.setWindowTitle('QSM owned mixed video fixture');self.resize(1280,720);self.image=None
  self.input=QtWidgets.QLineEdit(self);self.input.setGeometry(32,64,540,42);self.input.setFont(QtGui.QFont('DejaVu Sans Mono',16));self.input.textEdited.connect(self.edited)
  self.text=QtWidgets.QPlainTextEdit(self);self.text.setGeometry(32,144,540,430);self.text.setFont(QtGui.QFont('DejaVu Sans Mono',12));self.text.setPlainText('\n'.join('Line %03d: exact text ABCDEFG 0123456789 / colored pixels'%n for n in range(400)))
  self.decoder=QtCore.QProcess(self);self.data=bytearray();self.decoder.readyReadStandardOutput.connect(self.frames)
  self.decoder.start('ffmpeg',['-v','error','-re','-stream_loop','-1','-i','/tmp/qsm-exact-ui-clip.mp4','-an','-f','rawvideo','-pix_fmt','rgb24','-'])
 def edited(self,text):
  state['characters']=len(text);state['events'].append({'n':len(text),'at_ns':time.monotonic_ns()});self.update(QtCore.QRect(576,64,64,64))
 def frames(self):
  self.data.extend(bytes(self.decoder.readAllStandardOutput()));size=640*360*3
  while len(self.data)>=size:
   raw=bytes(self.data[:size]);del self.data[:size];self.image=QtGui.QImage(raw,640,360,640*3,QtGui.QImage.Format_RGB888).copy();state['video_frames']+=1
  self.update(QtCore.QRect(640,96,640,360))
 def paintEvent(self,event):
  p=QtGui.QPainter(self);p.fillRect(self.rect(),QtGui.QColor(238,238,238));p.setPen(QtGui.QColor(25,25,25));p.setFont(QtGui.QFont('DejaVu Sans',14));p.drawText(32,32,'Owned QSM mixed text / video test')
  p.fillRect(580,72,48,48,QtGui.QColor(*COLORS[state['characters']%len(COLORS)]));p.fillRect(582,74,4,4,QtGui.QColor(250,250,250));p.fillRect(622,114,4,4,QtGui.QColor(250,250,250))
  if self.image:p.drawImage(QtCore.QRect(640,96,640,360),self.image)
class Overlap(QtWidgets.QWidget):
 def __init__(self):
  super().__init__(None,QtCore.Qt.Window);self.setWindowTitle('QSM exact UI overlap');self.resize(640,360)
  self.input=QtWidgets.QLineEdit(self);self.input.setGeometry(16,48,540,42);self.input.setFont(QtGui.QFont('DejaVu Sans Mono',16));self.input.textEdited.connect(self.edited)
  self.text=QtWidgets.QPlainTextEdit(self);self.text.setGeometry(16,112,608,220);self.text.setFont(QtGui.QFont('DejaVu Sans Mono',12));self.text.setPlainText('\n'.join('Overlap %03d: clear text over moving video 0123456789'%n for n in range(400)))
  self.setAttribute(QtCore.Qt.WA_DeleteOnClose,False)
 def edited(self,text):
  state['characters']=len(text);state['events'].append({'n':len(text),'at_ns':time.monotonic_ns()});self.update(QtCore.QRect(568,40,64,64))
 def paintEvent(self,event):
  p=QtGui.QPainter(self);p.fillRect(self.rect(),QtGui.QColor(238,238,238));p.setPen(QtGui.QColor(20,20,20));p.drawText(16,28,'Text input window overlapping the running clip')
  p.fillRect(580,48,48,48,QtGui.QColor(*COLORS[state['characters']%len(COLORS)]));p.fillRect(582,50,4,4,QtGui.QColor(250,250,250));p.fillRect(622,90,4,4,QtGui.QColor(250,250,250))
base=Base();base.show();overlap=Overlap()
def sample():
 f=ROOT/'command.json'
 if f.exists():
  try:
   cmd=json.loads(f.read_text());f.unlink();op=cmd['op']
   if op=='overlap':overlap.show();overlap.raise_();overlap.input.setFocus();state['overlap']=True
   elif op=='hide':overlap.hide();state['overlap']=False
   elif op=='reset':
    w=overlap if state['overlap'] else base;w.input.clear();state['characters']=0;w.update();w.input.setFocus()
   elif op=='move_later':
    def moved():
     ipc=glob.glob('/run/qsm-display-1/sway-ipc.*.sock')[0];subprocess.run(['swaymsg','-s',ipc,'[title="QSM exact UI overlap"] move position 400 280'],stdout=subprocess.DEVNULL,check=True);state['moved_at_ns']=time.monotonic_ns()
    QtCore.QTimer.singleShot(1000,moved)
   elif op=='hide_later':
    def hidden():overlap.hide();state.update(overlap=False,hidden_at_ns=time.monotonic_ns())
    QtCore.QTimer.singleShot(1000,hidden)
   elif op=='snapshot':
    w=overlap if state['overlap'] else base;img=w.grab().toImage().convertToFormat(QtGui.QImage.Format_RGBA8888);b=img.bits();b.setsize(img.byteCount());(ROOT/'snapshot.rgba').write_bytes(bytes(b));state['snapshot_size']=[img.width(),img.height()]
  except Exception as e:state['error']=str(e)
 state['fixture_text_matches']=(overlap if state['overlap'] else base).input.text()=='abcdefghijklmno';state['scroll']=overlap.text.verticalScrollBar().value();state['base_size']=[base.width(),base.height()];state['overlap_size']=[overlap.width(),overlap.height()]
 temp=ROOT/'state.json.tmp';temp.write_text(json.dumps(state));temp.replace(ROOT/'state.json')
timer=QtCore.QTimer();timer.timeout.connect(sample);timer.start(20)
app.aboutToQuit.connect(lambda:base.decoder.kill())
sys.exit(app.exec_())

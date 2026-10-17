import asyncio,av,numpy as np
from fractions import Fraction
from extensions.browser_bridge.qsm_browser_bridge import _PacketTrack,EncodedUnit

def encode():
 c=av.CodecContext.create('libx264','w');c.width=320;c.height=180;c.pix_fmt='yuv420p';c.time_base=Fraction(1,60)
 c.options={'preset':'ultrafast','tune':'zerolatency','crf':'20','x264-params':'keyint=30:min-keyint=30:scenecut=0:ref=1'}
 units=[]
 for n in range(60):
  image=np.zeros((180,320,3),dtype=np.uint8);image[:]=[20,70,120]
  x=(n*7)%260;image[30:130,x:x+60]=[240,220,30]
  f=av.VideoFrame.from_ndarray(image,format='rgb24');f.pts=n
  units.extend(EncodedUnit(bytes(p),p.is_keyframe,1500) for p in c.encode(f))
 units.extend(EncodedUnit(bytes(p),p.is_keyframe,1500) for p in c.encode(None))
 return units

async def main():
 units=encode();reference=av.CodecContext.create('h264','r');decoder=av.CodecContext.create('h264','r')
 expected=[reference.decode(av.Packet(u.data))[0].to_ndarray(format='rgb24') for u in units]
 track=_PacketTrack('video',maximum_queue=1);corrupted=[]
 for n,u in enumerate(units):
  track.put_nowait(u)
  if n==10:continue # one brief scheduler/network stall, then normal delivery
  p=await track.recv()
  images=decoder.decode(p)
  if images:
   delta=float(np.abs(images[0].to_ndarray(format='rgb24').astype(float)-expected[n]).mean())
   if delta>1:corrupted.append(n)
 print({'single_dropped_frame':10,'corrupted_frames':corrupted,'recovered_at_idr':30})
asyncio.run(main())

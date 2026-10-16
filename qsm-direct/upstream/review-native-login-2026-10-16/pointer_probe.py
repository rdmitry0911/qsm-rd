"""Input-to-video cursor probe on an idle, disposable native greeter.

The sampled ROI contains no controls/clock. Native OS/CSS cursor rendering
is excluded by canvas.drawImage(video): only remote video pixels are timed.
This is not a measurement of a local hardware cursor's physical latency.
"""
import json
from pathlib import Path
from statistics import median

SETUP = r'''() => {
    const video = document.querySelector('video');
    if (!video?.videoWidth || !video.requestVideoFrameCallback) throw Error('video/rVFC required');
    const canvas = document.createElement('canvas');
    canvas.width=video.videoWidth; canvas.height=video.videoHeight;
    const ctx=canvas.getContext('2d',{willReadFrequently:true});
    const probe={video,canvas,ctx,pending:null};
    window.__qsmPointerProbe=probe;
    video.addEventListener('mousemove', event=>{
        if (probe.pending && probe.pending.inputAt===null) probe.pending.inputAt=performance.now();
    },true);
    const frame=(now,metadata)=>{
        const p=probe.pending;
        if(p && p.inputAt!==null){
            ctx.drawImage(video,0,0,canvas.width,canvas.height);
            const pixels=ctx.getImageData(p.x,p.y,48,56).data;
            let changed=0;
            for(let i=0;i<pixels.length;i+=4){
                if(Math.abs(pixels[i]-p.baseline[i])+Math.abs(pixels[i+1]-p.baseline[i+1])+
                   Math.abs(pixels[i+2]-p.baseline[i+2])>100) changed++;
            }
            if(changed>=35){
                clearTimeout(p.timer);probe.pending=null;
                p.resolve({input_to_video_ms:performance.now()-p.inputAt,input_epoch_ms:performance.timeOrigin+p.inputAt,changed_pixels:changed,
                    media_time:metadata.mediaTime,processing_ms:(metadata.processingDuration||0)*1000});
            }
        }
        video.requestVideoFrameCallback(frame);
    };
    video.requestVideoFrameCallback(frame);
    probe.arm=(x,y)=>{
        ctx.drawImage(video,0,0,canvas.width,canvas.height);
        const pending={x,y,inputAt:null,baseline:ctx.getImageData(x,y,48,56).data};
        probe.result=new Promise(resolve=>{pending.resolve=resolve;pending.timer=setTimeout(()=>{
            probe.pending=null;resolve({timeout:true,input_seen:pending.inputAt!==null});
        },2500);});
        probe.pending=pending;
    };
    return {width:video.videoWidth,height:video.videoHeight,cursor:getComputedStyle(video).cursor,
        receiver_features:{jitterBufferTarget:'jitterBufferTarget' in RTCRtpReceiver.prototype,
            playoutDelayHint:'playoutDelayHint' in RTCRtpReceiver.prototype}};
}'''

async def measure_pointer(popup, output):
    parent=await popup.opener()
    await parent.evaluate("""() => {
        const proto=RTCDataChannel.prototype,original=proto.send;
        window.__qsmPointerSendProbe={latest:null,restore:()=>{proto.send=original;}};
        proto.send=function(data){
            if(this.label==='qsm-pointer') window.__qsmPointerSendProbe.latest={
                epoch_ms:performance.timeOrigin+performance.now(),buffered_bytes:this.bufferedAmount};
            return original.call(this,data);
        };
    }""")
    setup=await popup.evaluate(SETUP)
    assert setup['width']>=800 and setup['height']>=600
    box=await popup.locator('video').bounding_box()
    # Greeter backdrop outside its avatar, login controls and clock.
    points=[(180,490),(setup['width']-200,490)]
    samples=[]
    await popup.mouse.move(box['x']+points[1][0],box['y']+points[1][1])
    await popup.wait_for_timeout(1500)
    await popup.evaluate('([x,y])=>window.__qsmPointerProbe.arm(x,y)',list(points[1]))
    await popup.mouse.move(box['x']+points[1][0],box['y']+points[1][1])
    negative=await popup.evaluate('()=>window.__qsmPointerProbe.result')
    assert negative.get('timeout') and negative.get('input_seen'), 'stationary input changed the video ROI'
    for iteration in range(12):
        x,y=points[iteration%2]
        await popup.evaluate('([x,y])=>window.__qsmPointerProbe.arm(x,y)',[x,y])
        await popup.mouse.move(box['x']+x,box['y']+y)
        sample=await popup.evaluate('()=>window.__qsmPointerProbe.result')
        sent=await parent.evaluate('()=>window.__qsmPointerSendProbe.latest')
        if sent and 'input_epoch_ms' in sample:
            sample['event_to_send_ms']=sent['epoch_ms']-sample.pop('input_epoch_ms')
            sample['sctp_buffered_bytes']=sent['buffered_bytes']
        samples.append(sample)
        await popup.wait_for_timeout(250)
    await parent.evaluate('()=>window.__qsmPointerSendProbe.restore()')
    values=sorted(s['input_to_video_ms'] for s in samples if 'input_to_video_ms' in s)
    result={'browser_version':popup.context.browser.version,'setup':setup,'stationary_control':negative,'samples':samples,'successes':len(values),
            'median_ms':median(values) if values else None,
            'p95_ms':values[min(len(values)-1,int(.95*len(values)))] if values else None,
            'limitation':'Headless browser on the lab client; not the user VPN/device or physical pointer latency.'}
    Path(output).write_text(json.dumps(result,indent=2)+'\n')
    print('POINTER_LATENCY',json.dumps({k:v for k,v in result.items() if k not in ('setup','samples')}),flush=True)

"""Measure actual DOM-mousemove to unordered channel send, without timing injection."""
import json,statistics,math

async def measure_dispatch(page,output):
    parent=await page.opener()
    await parent.evaluate('''() => {
        const original=RTCDataChannel.prototype.send;
        window.qsmDispatch={latest:null,input:null,mark(){this.input=performance.now();},restore(){RTCDataChannel.prototype.send=original;}};
        RTCDataChannel.prototype.send=function(data){
            if(this.label==='qsm-pointer')window.qsmDispatch.latest={time:performance.now(),buffered:this.bufferedAmount};
            return original.call(this,data);
        };
    }''')
    await page.evaluate('''()=>{
        window.qsmDispatchInput=()=>{window.opener.qsmDispatch.mark();};
        document.querySelector('video').addEventListener('mousemove',window.qsmDispatchInput,true);
    }''')
    samples=[]
    try:
        for i in range(20):
            await page.mouse.move(240 if i%2 else 640,300)
            await page.wait_for_timeout(35)
            event=await parent.evaluate('window.qsmDispatch.input')
            sent=await parent.evaluate('window.qsmDispatch.latest')
            samples.append({'event_to_send_ms':sent['time']-event,'buffered_bytes':sent['buffered']})
    finally:
        await parent.evaluate('window.qsmDispatch.restore()')
        await page.evaluate("document.querySelector('video').removeEventListener('mousemove',window.qsmDispatchInput,true)")
    values=sorted(s['event_to_send_ms'] for s in samples)
    result={'browser_version':page.context.browser.version,'samples':samples,'n':len(values),
            'median_ms':statistics.median(values),'p95_ms':values[math.ceil(.95*len(values))-1]}
    assert all(0<=v<25 for v in values)
    output.write_text(json.dumps(result,indent=2)+'\n')
    print('POINTER_DISPATCH',json.dumps({k:v for k,v in result.items() if k!='samples'}),flush=True)

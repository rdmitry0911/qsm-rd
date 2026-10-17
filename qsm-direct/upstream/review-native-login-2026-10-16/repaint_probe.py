"""DOM input -> actual guest Qt paint/window-move detection from video pixels."""
import json,statistics,math

async def measure_repaint(page, output):
    await page.evaluate('''() => {
        const v=document.querySelector('video'), c=document.createElement('canvas');
        c.width=v.videoWidth;c.height=v.videoHeight;
        const ctx=c.getContext('2d',{willReadFrequently:true});
        const p=window.qsmRepaintProbe={last:null,armed:null,done:null,css:[]};
        const input=e=>{if(p.armed&&p.armed.t0===null) p.armed.t0=performance.now();};
        v.addEventListener('mousemove',input,true);
        p.arm=(mode,target)=>{p.done=null;p.armed={mode,target,t0:null};};
        p.stop=()=>{p.stopped=true;v.removeEventListener('mousemove',input,true);};
        const frame=()=>{
            ctx.drawImage(v,0,0);
            const color=Array.from(ctx.getImageData(400,250,1,1).data);
            const row=ctx.getImageData(0,200,c.width,1).data;
            let edge=-1;
            for(let x=0;x<c.width;x++){if(row[x*4+2]>180&&((row[x*4]>180&&row[x*4+1]<70)||(row[x*4]<70&&row[x*4+1]<150))){edge=x;break;}}
            p.last={color,edge};
            const a=p.armed;
            if(a&&a.t0!==null) {
                const match=a.mode==='hover' ? (a.target ? color[0]>180&&color[2]>180 : color[0]<70&&color[2]>180) : Math.abs(edge-a.target)<=3;
                if(match){p.done={input_to_video_ms:performance.now()-a.t0,observed_edge:edge};p.armed=null;}
            }
            if(!p.stopped)v.requestVideoFrameCallback(frame);
        };v.requestVideoFrameCallback(frame);
    }''')
    await page.mouse.move(240,300)
    await page.wait_for_timeout(350)
    samples=[];shapes=[]
    for i in range(20):
        right=i%2==0
        await page.evaluate('target=>window.qsmRepaintProbe.arm("hover",target)',right)
        await page.mouse.move(640 if right else 240,300)
        await page.wait_for_function('window.qsmRepaintProbe.done!==null',timeout=5000)
        sample=await page.evaluate('window.qsmRepaintProbe.done')
        sample.update(kind='hover',iteration=i);samples.append(sample)
        await page.wait_for_timeout(180)
        css=await page.locator('video').evaluate('(v)=>v.style.cursor')
        shapes.append(css)
    # Keep magenta active while dragging the actual top-level Qt window.
    await page.mouse.move(640,300);await page.wait_for_timeout(350)
    await page.mouse.move(300,120);await page.mouse.down()
    for i,offset in enumerate([80,160,240,320,240,160,80,0]):
        await page.evaluate('edge=>window.qsmRepaintProbe.arm("drag",edge)',100+offset)
        await page.mouse.move(300+offset,120)
        await page.wait_for_function('window.qsmRepaintProbe.done!==null',timeout=5000)
        sample=await page.evaluate('window.qsmRepaintProbe.done')
        sample.update(kind='window_drag',iteration=i);samples.append(sample)
        await page.wait_for_timeout(180)
    await page.mouse.up()
    await page.evaluate('window.qsmRepaintProbe.stop()')
    result={'browser_version':page.context.browser.version,'samples':samples,'distinct_css_shapes':len(set(shapes)),
            'limitation':'CT108 pixman/libx264 over lab LAN, headless browser; not CT105 NVENC/VPN or physical display timing.'}
    assert result['distinct_css_shapes']>=2,'real guest arrow/I-beam shape did not change'
    for kind in ['hover','window_drag']:
        values=sorted(s['input_to_video_ms'] for s in samples if s['kind']==kind)
        result[kind]={'n':len(values),'median_ms':statistics.median(values),'p95_ms':values[math.ceil(.95*len(values))-1]}
    output.write_text(json.dumps(result,indent=2)+'\n')
    print('REPAINT_PROBE',json.dumps({k:v for k,v in result.items() if k!='samples'}),flush=True)

async def measure_fresh_frames(page, output):
    result=await page.evaluate('''() => new Promise(resolve=>{
        const v=document.querySelector('video'), c=document.createElement('canvas');c.width=1;c.height=1;
        const ctx=c.getContext('2d',{willReadFrequently:true});
        let previous=null,begin=null,frames=0,changed=0;const intervals=[];let lastChange=null;
        const frame=(now)=>{
            if(begin===null)begin=now;
            ctx.drawImage(v,130,180,1,1,0,0,1,1);
            const gray=ctx.getImageData(0,0,1,1).data[0];frames++;
            if(previous!==null&&Math.abs(gray-previous)>4){changed++;if(lastChange!==null)intervals.push(now-lastChange);lastChange=now;}
            previous=gray;
            if(now-begin<5000)v.requestVideoFrameCallback(frame);
            else resolve({duration_ms:now-begin,presented_frames:frames,fresh_frames:changed,fresh_fps:changed*1000/(now-begin),intervals_ms:intervals});
        };v.requestVideoFrameCallback(frame);
    })''')
    result['browser_version']=page.context.browser.version
    output.write_text(json.dumps(result,indent=2)+'\n')
    print('FRESH_FRAMES',json.dumps({k:v for k,v in result.items() if k!='intervals_ms'}),flush=True)

async def measure_cursor_shapes(page, output):
    shapes=[]
    for x,y in [(240,300),(640,300),(640,450),(240,300)]:
        await page.mouse.move(x,y);await page.wait_for_timeout(350)
        shapes.append(await page.locator('video').evaluate('(v)=>v.style.cursor'))
    await page.locator('video').screenshot(path=str(output.with_suffix('.png')))
    import hashlib
    print('CURSOR_SHAPE_DIAGNOSTIC',json.dumps([{'kind':s.split('(')[0],'hash':hashlib.sha256(s.encode()).hexdigest()} for s in shapes]),flush=True)
    assert shapes[0].startswith('url(') and shapes[1].startswith('url(') and shapes[0]!=shapes[1], 'arrow/I-beam CSS shape did not change' 
    hidden=shapes[2]=='none'
    if not hidden:
        hidden=await page.evaluate(r"""async css=>{
            const match=css.match(/url\("([^"]+)"\)/);if(!match)return false;
            const img=new Image();img.src=match[1];await img.decode();
            const c=document.createElement('canvas');c.width=img.width;c.height=img.height;
            const ctx=c.getContext('2d');ctx.drawImage(img,0,0);
            const pixels=ctx.getImageData(0,0,c.width,c.height).data;
            return Array.from(pixels).every((value,index)=>index%4!==3||value===0);
        }""",shapes[2])
    assert hidden,'blank guest cursor must hide the native cursor' 
    assert shapes[3]==shapes[0],'arrow must return after a hidden cursor'
    result={'browser_version':page.context.browser.version,'arrow':True,'ibeam':True,'hidden':True,'restored':True}
    output.write_text(json.dumps(result,indent=2)+'\n');print('CURSOR_SHAPES',json.dumps(result),flush=True)

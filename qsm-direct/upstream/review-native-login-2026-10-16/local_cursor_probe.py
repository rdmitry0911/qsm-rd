"""Verify that the real guest cursor is CSS-rendered and absent from video."""
import asyncio, hashlib, json

async def measure_local_cursor(page, output):
    await page.evaluate('''() => {
        const video = document.querySelector('video');
        const canvas = document.createElement('canvas');
        canvas.width = video.videoWidth; canvas.height = video.videoHeight;
        const ctx = canvas.getContext('2d', {willReadFrequently: true});
        window.qsmCursorProbe = {samples: [], previous: null, changes: []};
        const probe = window.qsmCursorProbe;
        const frame = () => {
            ctx.drawImage(video, 0, 0);
            const pixels = [180, video.videoWidth - 200].flatMap(x =>
                Array.from(ctx.getImageData(x-16, 470, 48, 56).data));
            if (probe.previous) {
                let changed = 0;
                for (let i = 0; i < pixels.length; i += 4) {
                    if (Math.abs(pixels[i]-probe.previous[i]) +
                        Math.abs(pixels[i+1]-probe.previous[i+1]) +
                        Math.abs(pixels[i+2]-probe.previous[i+2]) > 100) changed++;
                }
                probe.changes.push(changed);
            }
            probe.previous = pixels;
            if (!probe.stop) video.requestVideoFrameCallback(frame);
        };
        video.requestVideoFrameCallback(frame);
    }''')
    box=await page.locator('video').bounding_box()
    dimensions=await page.locator('video').evaluate('(v)=>({width:v.videoWidth,height:v.videoHeight})')
    for i in range(12):
        x=180 if i%2==0 else dimensions['width']-200
        await page.mouse.move(box['x']+x*box['width']/dimensions['width'],box['y']+490*box['height']/dimensions['height'])
        await page.wait_for_timeout(250)
    result=await page.evaluate('''() => {
        const probe = window.qsmCursorProbe; probe.stop = true;
        return {frames: probe.changes.length, max_changed_pixels: Math.max(0, ...probe.changes),
            cursor: document.querySelector('video').style.cursor};
    }''')
    css=result.pop('cursor')
    sprite=await page.evaluate(r"""async css=>{
        const match=css.match(/url\("([^"]+)"\)/);if(!match)return null;
        const img=new Image();img.src=match[1];await img.decode();
        const c=document.createElement('canvas');c.width=img.width;c.height=img.height;
        const ctx=c.getContext('2d');ctx.drawImage(img,0,0);
        const data=ctx.getImageData(0,0,c.width,c.height).data;
        let visible=0;for(let i=3;i<data.length;i+=4)if(data[i])visible++;
        return {width:img.width,height:img.height,visible_pixels:visible};
    }""",css)
    result['sprite']=sprite
    assert sprite and sprite['visible_pixels']>10,'cursor sprite is empty or the legacy transparent marker'
    result['css_cursor_sha256']=hashlib.sha256(css.encode()).hexdigest()
    result['native_css_cursor']=css.startswith('url("data:image/png;')
    result['video_cursor_absent']=result['frames']>10 and result['max_changed_pixels']<35
    assert result['native_css_cursor'], 'guest sprite did not reach native CSS cursor'
    assert result['video_cursor_absent'], 'moving pointer is still visible in video ROI'
    result['browser_version']=page.context.browser.version
    result['positions']=12
    output.write_text(json.dumps(result,indent=2)+'\n')
    print('LOCAL_CURSOR_PROBE',json.dumps(result),flush=True)

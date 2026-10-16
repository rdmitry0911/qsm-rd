#!/usr/bin/env python3
"""Interactive native PVE/SDDM qualification driver for isolated CT108.

Requires Playwright and SSH root access to the lab node. Ephemeral
account passwords stay in the selected CT's /root and are read
only into memory. No credentials are written to traces or screenshots.
See qualification.txt for the public JSON commands used.
"""
import asyncio,json,subprocess,sys,os,re
from pathlib import Path
from playwright.async_api import async_playwright
HOST='192.168.64.20'
VMID=int(os.environ.get('QSM_LAB_VMID','108'))
assert VMID in (105,108)
def ssh(command):
    return subprocess.run(['ssh','-o','BatchMode=yes','root@'+HOST,command],check=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout
async def main():
    Path(os.environ.get('QSM_LAB_ARTIFACTS','/tmp/qsm-lxc-native-login')).mkdir(parents=True,exist_ok=True)
    ticket=ssh("perl -MPVE::AccessControl -MPVE::Cluster -e 'PVE::Cluster::cfs_update(); print scalar PVE::AccessControl::assemble_ticket(q{root@pam})'")
    account=os.environ.get('QSM_TEST_ACCOUNT','qsm-test-a')
    assert account in ('qsm-test-a','qsm-test-b','qsm-layout-check','none')
    secret=ssh(f'pct exec {VMID} -- cat /root/'+account+'.password').strip() if account!='none' else None
    async with async_playwright() as p:
        if os.environ.get('QSM_LAB_BROWSER')=='firefox':
            browser=await p.firefox.launch(headless=True,env=dict(os.environ),firefox_user_prefs={
                'media.gmp-manager.updateEnabled':True,'media.gmp-provider.enabled':True,
                'media.gmp-gmpopenh264.enabled':True,'media.peerconnection.video.h264_enabled':True})
        else:
            browser=await p.chromium.launch(executable_path=os.environ.get('QSM_CHROMIUM_EXECUTABLE',p.chromium.executable_path),headless=True,args=['--autoplay-policy=no-user-gesture-required'])
        try:
            context=await browser.new_context(ignore_https_errors=True,viewport={'width':1280,'height':800})
            legacy_requests=[]
            context.on('request', lambda request: legacy_requests.append(True) if ':8007/' in request.url else None)
            await context.route('**:8007/**', lambda route: route.abort())
            try:await context.grant_permissions(['local-network-access'],origin='https://'+HOST+':8006')
            except Exception:pass
            page=await context.new_page()
            print('STAGE_PVE_LOGIN',flush=True)
            await page.goto('https://'+HOST+':8006/',wait_until='domcontentloaded')
            await page.locator('input[name=username]:visible').fill('root')
            await page.locator('input[name=password]:visible').fill(ticket)
            await page.evaluate("()=>{Ext.ComponentQuery.query('field[name=realm]')[0].setValue('pam');}")
            await page.get_by_role('button',name='Login',exact=True).click()
            print('STAGE_PVE_CAPS',flush=True)
            await page.wait_for_function("Ext.state.Manager.get('GuiCap')?.mapping",timeout=30000)
            notices=page.locator('.x-message-box:visible')
            if await notices.count():
                await notices.locator('.x-btn').filter(has_text='OK').last.click()
            await page.wait_for_function("(vmid)=>Ext.ComponentQuery.query('pveResourceTree')[0]?.getStore().getRootNode().findChild('id','lxc/'+vmid,true)",arg=VMID,timeout=30000)
            await page.evaluate("(vmid)=>{Ext.ComponentQuery.query('pveResourceTree')[0].selectById('lxc/'+vmid);}",VMID)
            identity=await page.wait_for_function("(vmid)=>Ext.ComponentQuery.query('pveConsoleButton').find(b=>b.consoleType==='lxc'&&Number(b.vmid)===vmid&&b.rendered&&b.enableQsmDirect)?.getEl().dom.id",arg=VMID,timeout=30000)
            await page.evaluate('(id)=>{Ext.getCmp(id).showMenu();}',await identity.json_value())
            async with page.expect_popup() as event:
                await page.locator('.x-menu:visible').get_by_text('QSM Direct',exact=True).click()
            popup=await event.value
            print('STAGE_NATIVE_VIDEO',flush=True)
            popup.on('close',lambda:print('POPUP_CLOSED',flush=True))
            page.on('pageerror',lambda e:print('PAGE_ERROR',e.name,flush=True))
            await popup.wait_for_function("document.querySelector('video')?.videoWidth>0",timeout=40000)
            await popup.wait_for_timeout(1000)
            print('NATIVE_VIDEO',await popup.locator('video').evaluate('(v)=>({width:v.videoWidth,height:v.videoHeight})'),flush=True)
            while True:
                line=await asyncio.to_thread(sys.stdin.readline)
                if not line:break
                command=json.loads(line)
                op=command['op']
                if op=='snapshot':
                    name=command['name'];assert re.fullmatch('[a-z0-9-]+',name)
                    await popup.locator('video').screenshot(path=str(Path(os.environ.get('QSM_LAB_ARTIFACTS','/tmp/qsm-lxc-native-login'))/(name+'.png')))
                    print('SNAPSHOT',name,flush=True)
                elif op=='click':
                    box=await popup.locator('video').bounding_box()
                    await popup.mouse.click(box['x']+command['x'],box['y']+command['y'])
                    print('CLICK_OK',flush=True)
                elif op=='wheel':
                    await popup.mouse.wheel(0,command['dy']);print('WHEEL_OK',flush=True)
                elif op=='focus':
                    await popup.locator('video').focus();print('FOCUS_OK',flush=True)
                elif op=='keys':
                    for key in command['keys']:await popup.keyboard.press(key)
                    print('KEYS_OK',flush=True)
                elif op=='type':
                    assert command['value'] in ['qsm-test-a','qsm-test-b','invalid-qsm-test','qsm-layout-check']
                    await popup.keyboard.type(command['value'],delay=30);print('TEXT_OK',flush=True)
                elif op=='password':
                    assert secret is not None, 'greeter-only mode has no credentials'
                    for ch in secret:
                        if ch.isupper(): key='Shift+Key'+ch
                        elif ch.islower(): key='Key'+ch.upper()
                        elif ch.isdigit(): key='Digit'+ch
                        elif ch=='_': key='Shift+Minus'
                        elif ch=='-': key='Minus'
                        else: raise ValueError('unexpected test password character')
                        await popup.keyboard.press(key)
                    print('PASSWORD_TYPED',flush=True)
                elif op=='wait':await popup.wait_for_timeout(1500);print('WAIT_OK',flush=True)
                elif op=='quit':
                    assert not legacy_requests, 'Unexpected legacy signalling request'
                    print('NO_PORT_8007_REQUESTS',flush=True)
                    break
        except Exception as error:
            message=str(error).replace(ticket,'[ticket]')
            if secret:message=message.replace(secret,'[password]')
            print('BROWSER_ERROR',message,flush=True)
            raise RuntimeError('browser native-login qualification failed') from None
        finally:await browser.close()
asyncio.run(main())

import asyncio,json,os,sys
from playwright.async_api import async_playwright
async def main():
 async with async_playwright() as p:
  which=sys.argv[1];launch=p.firefox if which=='firefox' else p.chromium
  args={'headless':os.environ.get('QSM_HEADED')!='1'}
  if which=='firefox':args['firefox_user_prefs']={'media.peerconnection.ice.obfuscate_host_addresses':False,'media.gmp-gmpopenh264.enabled':True,'media.peerconnection.video.h264_enabled':True}
  else:args.update(executable_path='/usr/bin/google-chrome',args=['--autoplay-policy=no-user-gesture-required']+json.loads(os.environ.get('QSM_CHROME_ARGS','[]')))
  b=await launch.launch(**args)
  page=await b.new_page(ignore_https_errors=True)
  await page.goto('https://localhost:18006',wait_until='domcontentloaded')
  await page.wait_for_timeout(2000)
  result=await page.evaluate('''async()=>{
   const pc=new RTCPeerConnection();pc.addTransceiver('video',{direction:'recvonly'});const offer=await pc.createOffer();pc.close();
   const configs={};for(const codec of ['hvc1.1.6.L120.B0','hev1.1.6.L120.B0','avc1.42001f']){
    try{configs[codec]=await VideoDecoder.isConfigSupported({codec,codedWidth:1280,codedHeight:800})}catch(e){configs[codec]={error:e.name}}}
   return {userAgent:navigator.userAgent,receiver:RTCRtpReceiver.getCapabilities('video'),sender:RTCRtpSender.getCapabilities('video'),offer:offer.sdp,webcodecs:configs,mediaHevc:document.createElement('video').canPlayType('video/mp4; codecs="hvc1.1.6.L120.B0"')};
  }''')

  if which!='firefox':
   cdp=await b.new_browser_cdp_session();result['gpu']=await cdp.send('SystemInfo.getInfo')
  print(json.dumps(result,indent=2));await b.close()
asyncio.run(main())

#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Token-mode console under an emulated lossy path: input snapshots, browser data-channel counters and aiortc SCTP receive state."""
import asyncio, gc, os, secrets, socket, subprocess, sys, tempfile, time
ROOT, WORKER, BROWSER, ADDR, DURATION = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5])
sys.path.insert(0, ROOT)
from playwright.async_api import async_playwright
from extensions.standalone import server
async def main():
    tmp = tempfile.mkdtemp(prefix="qsm-sctp-")
    tok = os.path.join(tmp, "tok"); fd = os.open(tok, os.O_WRONLY | os.O_CREAT, 0o600); token = secrets.token_urlsafe(32); os.write(fd, (token + "\n").encode()); os.close(fd)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    args = server.arguments(["--auth", "token", "--dbus-address", ADDR, "--token-file", tok, "--worker", WORKER, "--port", str(port), "--fps", "30", "--name", "anim"])
    task = asyncio.create_task(server.run(args)); await asyncio.sleep(1)
    t0 = time.monotonic(); log = lambda m: print(f"PROBE +{time.monotonic()-t0:6.1f}s {m}", flush=True)
    def sctp_state():
        from aiortc.rtcsctptransport import RTCSctpTransport
        out = []
        for o in gc.get_objects():
            if isinstance(o, RTCSctpTransport):
                g = lambda n: getattr(o, n, None)
                out.append(f"state={g('_association_state')} cwnd={g('_cwnd')} flight={g('_flight_size')} rwnd={g('_rwnd')} "
                           f"outq={len(g('_outbound_queue') or [])} sentq={len(g('_sent_queue') or [])} rto={g('_rto')} "
                           f"t3={'on' if g('_t3_handle') else 'off'} last_rx_tsn={g('_last_received_tsn')} misordered={sorted(g('_sack_misordered') or [])[:6]} "
                           f"n_misordered={len(g('_sack_misordered') or [])} dups={len(g('_sack_duplicates') or [])} "
                           f"streams={{{', '.join(f'{k}:seq{v.sequence_number}/q{len(v.reassembly)}' for k, v in (g('_inbound_streams') or {}).items())}}}")
        return out
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=BROWSER, headless=True, args=["--no-sandbox"])
            ctx = await b.new_context(viewport={"width": 1600, "height": 900})
            await ctx.add_init_script("""(() => { const O = window.RTCPeerConnection; window.__pcs = []; window.RTCPeerConnection = function (...a) { const pc = new O(...a); window.__pcs.push(pc); return pc; }; window.RTCPeerConnection.prototype = O.prototype; })()""")
            page = await ctx.new_page()
            page.on("console", lambda m: print("CONSOLE", m.type, m.text[:200], flush=True))
            page.on("pageerror", lambda e: print("PAGEERROR", str(e)[:200], flush=True))
            await page.goto(f"http://127.0.0.1:{port}/"); await page.fill("#token", token)
            await page.get_by_role("button", name="Connect", exact=True).click()
            try:
                await page.wait_for_function("() => { const v = document.querySelector('video'); return v && v.readyState >= 2 && v.currentTime > 0; }", timeout=60000)
            except Exception:
                print("BODY", (await page.evaluate("() => document.body.innerText"))[:500], flush=True); raise
            log("video playing")
            await page.evaluate("() => document.documentElement.requestFullscreen().catch(() => {})")
            end = time.monotonic() + DURATION; step = 0
            while time.monotonic() < end:
                await page.mouse.move(200 + (step % 50) * 20, 200 + (step % 30) * 15); step += 1
                if step % 20 == 0:
                    st = await page.evaluate("""async () => { const out = []; for (const pc of (window.__pcs || [])) { const r = await pc.getStats(); r.forEach(v => { if (v.type === 'data-channel') out.push(`dc ${v.label} ${v.state} sent=${v.messagesSent} recv=${v.messagesReceived}`); if (v.type === 'transport') out.push(`tr ${v.dtlsState} pkts_sent=${v.packetsSent} pkts_recv=${v.packetsReceived}`); }); } return out.join(' | '); }""")
                    log(f"browser {st}")
                    log(f"sctp {sctp_state()}")
                await asyncio.sleep(.25)
            await b.close()
    finally:
        os.kill(os.getpid(), 15); await task
asyncio.run(main())

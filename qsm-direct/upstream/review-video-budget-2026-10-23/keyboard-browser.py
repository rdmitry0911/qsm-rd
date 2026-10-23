#!/usr/bin/env python3
"""Exercise production keyboard handlers with real DOM focus in Firefox/Chromium.

Uses Playwright; records control-channel edges, without a production login.
The same focused fixture can exercise a pre-fix checkout for comparison.
"""
import asyncio
import json
from pathlib import Path
import sys

from playwright.async_api import async_playwright


def fragment(source, start, end):
    a = source.index(start)
    return source[a:source.index(end, a)]


async def main():
    source = Path(sys.argv[1]).read_text()
    script = """
        const video = document.querySelector('video');
        const kbInput = document.querySelector('#keyboard');
        const heldKeys = new Set(), messages = [];
        const send = value => { messages.push(value); return true; };
        const noteGuestInput = () => {};
        let diagKeySent = 0, diagLastInput = '', fullscreenEscapePending = false;
        const KEYBOARD_PRIORITY = {client:'client',raw:'raw'};
        const settings = {keyboardPriority:'guest'};
        const guestCaptureExitShortcut = () => false, clientFirstShortcut = () => false;
        const leaveGuestKeyboardCapture = () => {}, releaseHeldInput = () => {};
        const pasteFromBrowser = () => Promise.resolve(), guestSelectionToBrowser = () => Promise.resolve();
        const status = {}, gettext = text => text;
    """
    script += fragment(source, 'const scanCodes =', '// A guest-first console')
    script += fragment(source, 'const sendKeyboard =', '// Map a printable character')
    script += fragment(source, "for (const name of ['keydown', 'keyup']) {\n                video.addEventListener", "peer.addTransceiver('video'")
    script += 'window.edges = () => messages.map(({key,down}) => [key,down]);'
    results = []
    async with async_playwright() as p:
        for name, browser_type, executable in [
            ('firefox', p.firefox, '/home/dima/.cache/ms-playwright/firefox-1555/firefox/firefox'),
            ('chromium', p.chromium, '/home/dima/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome'),
        ]:
            browser = await browser_type.launch(executable_path=executable, headless=True)
            page = await browser.new_page()
            await page.set_content('<video tabindex="0"></video><input id="keyboard"><input id="local">')
            await page.add_script_tag(content=script)
            await page.locator('video').focus()
            await page.keyboard.down('a')
            await page.locator('#local').focus()
            after_blur = await page.evaluate('edges()')
            assert after_blur == [[30, True], [30, False]], after_blur
            await page.locator('video').focus()
            # Repeated down while physically held has repeat=true in both engines.
            await page.keyboard.down('a')
            assert await page.evaluate('edges()') == after_blur
            await page.keyboard.up('a')
            await page.keyboard.press('a')
            await page.locator('#local').focus()
            await page.keyboard.down('a')
            await page.locator('video').focus()
            await page.keyboard.down('a')
            await page.keyboard.up('a')
            edges = await page.evaluate('edges()')
            assert edges == [[30, True], [30, False]] * 2, edges
            results.append({'browser': name, 'version': browser.version,
                            'focus_release': 'pass', 'foreign_repeat': 'pass',
                            'fresh_typing': 'pass', 'edges': edges})
            await browser.close()
    print(json.dumps({'scope': 'production JS handlers; real DOM; recorded transport edges',
                      'results': results}, indent=2))


asyncio.run(main())

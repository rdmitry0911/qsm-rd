// Exercise the real cursor renderer with native, hidden and touch cursor states.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../../integration/proxmox/pve9/direct_ui/qsm-direct-console.js', import.meta.url), 'utf8');
const begin = source.indexOf('        const placeGuestCursor = () => {');
const end = source.indexOf('        const acceptGuestCursorShape =', begin);
assert.ok(begin > 0 && end > begin);
const style = () => ({ display: '', cursor: '' });
const context = vm.createContext({
    latestGuestCursor: { visible: true, shape_id: 7, x: 90, y: 80 },
    guestCursorShapes: new Map([[7, { cursor_url: 'data:image/png;base64,fixture', width: 32, height: 32, hotspot_x: 3, hotspot_y: 5 }]]),
    video: { videoWidth: 1280, videoHeight: 800, style: style() },
    guestCursor: { style: style() }, guestPointer: { style: style() },
    appliedGuestCursor: 'default', usingTouchInput: false,
    lastSentGuestX: 200, lastSentGuestY: 300,
    viewMetrics: () => ({ contain: 1 }), zoomScale: 1,
    guestToScreen: (x, y) => ({ x, y }),
    hideGuestCursor() { context.video.style.cursor = 'default'; },
});
vm.runInContext(source.slice(begin, end) + '\nglobalThis.renderCursor = placeGuestCursor;', context);
context.renderCursor();
assert.equal(context.video.style.cursor, 'url("data:image/png;base64,fixture") 3 5, default');
// Moving the local mouse must not wait for a returned guest position.
context.latestGuestCursor.x = 950;
context.renderCursor();
assert.equal(context.guestPointer.style.display, 'none');
context.latestGuestCursor.visible = false;
context.renderCursor();
assert.equal(context.video.style.cursor, 'none', 'explicitly hidden guest cursor must hide the OS cursor');
context.latestGuestCursor.visible = true;
context.renderCursor();
assert.match(context.video.style.cursor, /^url/);
context.usingTouchInput = true;
context.renderCursor();
assert.equal(context.guestPointer.style.left, '197px', 'touch sprite follows the most recent local input');
assert.equal(context.guestPointer.style.top, '295px');
context.latestGuestCursor.visible = false;
context.renderCursor();
assert.equal(context.guestPointer.style.display, 'none');
context.latestGuestCursor = null;
context.usingTouchInput = false;
context.renderCursor();
assert.equal(context.video.style.cursor, 'default', 'missing metadata retains the normal fallback');
console.log('QSM_LOCAL_CURSOR_OK');

/* SPDX-License-Identifier: AGPL-3.0-or-later
 * QSM advisory desktop geometry. KWin 5/6; no captions or application text.
 */
(function () {
    const service = 'org.qsm.DesktopMetadata';
    const path = '/org/qsm/DesktopMetadata';
    const iface = 'org.qsm.DesktopMetadata1';
    const connections = new Map();
    const windows = () => {
        if (workspace.stackingOrder) { return workspace.stackingOrder; }
        // KWin 5 exposes the stack index on each Window; clientList() is
        // creation order and must never be treated as occlusion order.
        const all = workspace.windowList ? workspace.windowList() : workspace.clientList();
        return all.filter(w => !!w).sort((a, b) => Number(a.stackingOrder) - Number(b.stackingOrder));
    };
    const id = w => String(w.internalId);
    const rect = r => [Math.floor(r.x), Math.floor(r.y), Math.ceil(r.width), Math.ceil(r.height)];
    const send = (method, value) => callDBus(service, path, iface, method, value);
    const on = (signal, fn, list) => {
        if (!signal || typeof signal.connect !== 'function') { return; }
        signal.connect(fn);
        if (list) { list.push([signal, fn]); }
    };
    const visible = w => !w.deleted && !w.minimized && !w.hidden &&
        (w.onCurrentDesktop !== false) && (w.onCurrentActivity !== false) && w.opacity !== 0;
    const clientRect = w => {
        // KWin 5 has no scriptable clientGeometry property. For server-side
        // decorations the surface buffer is the client area. Buffers which
        // extend outside the frame (e.g. CSD shadows) have an ambiguous
        // text-coordinate origin: retain window hints but omit that origin.
        const f = w.frameGeometry, c = w.clientGeometry || w.bufferGeometry;
        return c && c.x >= f.x && c.y >= f.y && c.x + c.width <= f.x + f.width &&
            c.y + c.height <= f.y + f.height ? rect(c) : null;
    };
    const publish = () => {
        const all = windows();
        const active = workspace.activeWindow || workspace.activeClient;
        const geometry = workspace.virtualScreenGeometry ||
            { x: 0, y: 0, width: workspace.displayWidth, height: workspace.displayHeight };
        const current = workspace.currentDesktop;
        const list = [];
        for (const w of all) {
            if (!w || w.deleted || !w.frameGeometry || w.frameGeometry.width <= 0 || w.frameGeometry.height <= 0) { continue; }
            let shown = visible(w);
            if (typeof current === 'number' && current > 0 && Number(w.desktop) > 0) {
                shown = shown && Number(w.desktop) === current;
            }
            if (current && current.id !== undefined && w.desktops && w.desktops.length) {
                shown = shown && w.desktops.some(d => d.id === current.id);
            }
            if (w.activities && w.activities.length && workspace.currentActivity) {
                shown = shown && w.activities.indexOf(workspace.currentActivity) >= 0;
            }
            list.push({ id: id(w), pid: Number(w.pid) || 0, rect: rect(w.frameGeometry),
                client_rect: clientRect(w),
                visible: shown, opacity: Number(w.opacity) });
        }
        // Overflow means unavailable, never a falsely complete window stack.
        if (list.length > 128) {
            send('Publish', JSON.stringify({ desktop: { rect: rect(geometry), scale: 1 },
                windows: [], focus: null, locked: false, available: false }));
            return;
        }
        const focus = active && list.find(w => w.id === id(active) && w.visible);
        let scale = 1;
        const outputs = workspace.screens;
        if (outputs && outputs.length === 1 && Number(outputs[0].scale) > 0) { scale = Number(outputs[0].scale); }
        send('Publish', JSON.stringify({ desktop: { rect: rect(geometry), scale }, windows: list,
            focus: focus ? focus.id : null, locked: false, available: true }));
    };
    const watch = w => {
        if (!w) { return; }
        const key = id(w);
        if (connections.has(key)) { return; }
        const hooks = [];
        connections.set(key, hooks);
        for (const name of ['frameGeometryChanged', 'clientGeometryChanged', 'visibleGeometryChanged',
            'minimizedChanged', 'hiddenChanged', 'windowShown', 'windowHidden', 'opacityChanged',
            'desktopChanged', 'desktopsChanged', 'activitiesChanged', 'stackingOrderChanged']) {
            on(w[name], publish, hooks);
        }
        on(w.damaged, () => send('Damage', key), hooks);
    };
    const remove = w => {
        if (!w) { publish(); return; }
        const key = id(w), hooks = connections.get(key) || [];
        for (const [signal, fn] of hooks) { try { signal.disconnect(fn); } catch (_e) {} }
        connections.delete(key); publish();
    };
    on(workspace.windowAdded || workspace.clientAdded, w => { watch(w); publish(); });
    on(workspace.windowRemoved || workspace.clientRemoved, remove);
    on(workspace.windowActivated || workspace.clientActivated, publish);
    for (const name of ['stackingOrderChanged', 'currentDesktopChanged', 'currentActivityChanged',
        'virtualScreenGeometryChanged', 'screensChanged']) { on(workspace[name], publish); }
    for (const w of windows()) { watch(w); }
    publish();
    // Asynchronous lease: an unloaded/failed script must not leave live
    // geometry behind just because the bridge process is still running.
    const pulse = () => callDBus(service, path, iface, 'Pulse', alive => { if (alive === true) { pulse(); } });
    pulse();
}());

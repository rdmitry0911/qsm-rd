// SPDX-License-Identifier: GPL-3.0-or-later
// Guest-side scenario switch for the QSM/noVNC comparison fixture.
//
// Every scenario page includes this file.  The measuring browser changes the
// guest workload through the console under test itself — the same key
// events a person would send — so no side channel into the guest is needed
// and QSM Direct and stock noVNC are switched by exactly the same action.
//
//   Alt+1  input-to-pixel fixture (hover popup, drag card, password field)
//   Alt+2  looping 1280×720 H.264 video clip
//   Alt+3  long text document scrolling itself at a reading pace
//   Alt+4  animated 2D canvas scene
//   Alt+5  the same document held still, for wheel-direction checks
'use strict';
(() => {
    const SCENARIOS = { 1: 'index.html', 2: 'video.html', 3: 'document.html', 4: 'motion.html', 5: 'document.html?manual' };
    window.addEventListener('keydown', (event) => {
        const target = event.altKey && !event.ctrlKey && !event.metaKey ? SCENARIOS[event.key] : null;
        if (!target) { return; }
        event.preventDefault();
        event.stopPropagation();
        const [file, query = ''] = target.split('?');
        if (!location.pathname.endsWith(`/${file}`) || location.search.slice(1) !== query) { location.replace(target); }
    }, true);
})();

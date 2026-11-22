/* SPDX-License-Identifier: AGPL-3.0-or-later */
'use strict';
(() => {
    // Keep the credential in this page's closure, never in a URL, cookie or
    // persistent browser storage. A refresh deliberately asks for it again.
    const form = document.querySelector('#login');
    let session = null;
    let token = '';
    const request = async (method, path, body, keepalive = false) => {
        const response = await fetch(path, {
            method, cache: 'no-store', credentials: 'omit', keepalive,
            signal: keepalive ? undefined : AbortSignal.timeout(30000),
            headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
            ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        if (!response.ok) {
            throw new Error(response.status === 401 ? 'Invalid access token.' : await response.text());
        }
        return response.status === 204 ? null : (await response.json()).data;
    };
    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const button = form.querySelector('button');
        button.disabled = true;
        token = document.querySelector('#token').value.trim();
        try {
            const config = await request('GET', '/api/status');
            window.gettext = message => message;
            window.ngettext = (one, many, n) => n === 1 ? one : many;
            window.QSM_WEBRTC_VIEWER = {
                standalone: true, name: config.name, fps: config.fps,
                // The shared viewer's transport is supplied by this entry;
                // no Proxmox classes, node identity or VMID are needed.
                transport: {
                    status: () => request('GET', '/api/status'),
                    offer: async params => {
                        const answer = await request('POST', '/api/offer', params);
                        session = answer.session;
                        return { type: answer.type, sdp: answer.sdp };
                    },
                    close: () => {
                        if (!session) { return; }
                        const id = session;
                        session = null;
                        request('DELETE', `/api/console/${id}`, undefined, true).catch(() => {});
                    },
                },
            };
            document.querySelector('#token').value = '';
            const script = document.createElement('script');
            script.src = '/console.js';
            script.onload = () => form.closest('main').remove();
            script.onerror = () => {
                document.querySelector('#error').textContent = 'Could not load the QSM console. Reload to retry.';
                button.disabled = false;
            };
            document.head.append(script);
        } catch (error) {
            document.querySelector('#error').textContent = error.message;
            button.disabled = false;
            token = '';
        }
    });
})();

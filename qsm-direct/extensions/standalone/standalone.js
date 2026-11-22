/* SPDX-License-Identifier: AGPL-3.0-or-later */
'use strict';
(() => {
    let mode = 'pam';
    let csrf = '';
    let token = '';
    let mediaSession = null;
    const main = document.querySelector('main');
    const error = document.querySelector('#error');
    const form = document.querySelector('#login');
    const guestList = document.querySelector('#guests');
    const request = async (method, path, body, keepalive = false) => {
        const headers = { 'Content-Type': 'application/json' };
        if (mode === 'token') { headers.Authorization = `Bearer ${token}`; }
        else if (method !== 'GET') { headers['X-QSM-CSRF'] = csrf; }
        const response = await fetch(path, {
            method, cache: 'no-store', credentials: mode === 'pam' ? 'same-origin' : 'omit', keepalive,
            signal: keepalive ? undefined : AbortSignal.timeout(45000), headers,
            ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
        if (!response.ok) {
            const failure = new Error(await response.text());
            failure.status = response.status;
            throw failure;
        }
        return response.status === 204 ? null : (await response.json()).data;
    };
    const showLogin = () => {
        form.hidden = false;
        guestList.hidden = true;
        document.querySelector('#intro').textContent = mode === 'pam'
            ? 'Sign in with your account on the QEMU host.' : 'Enter the access token from the console server.';
        for (const name of ['username', 'password']) {
            const input = document.querySelector(`#${name}`);
            input.required = mode === 'pam';
            input.closest('label').hidden = mode !== 'pam';
        }
        const input = document.querySelector('#token');
        input.required = mode === 'token';
        input.closest('label').hidden = mode !== 'token';
        form.querySelector('button').textContent = mode === 'pam' ? 'Sign in' : 'Connect';
    };
    const startConsole = async guest => {
        const prefix = mode === 'pam' ? `/api/guests/${encodeURIComponent(guest)}` : '/api';
        const config = await request('GET', `${prefix}/status`);
        window.gettext = message => message;
        window.ngettext = (one, many, n) => n === 1 ? one : many;
        window.QSM_WEBRTC_VIEWER = {
            standalone: true, name: config.name, fps: config.fps, home: mode === 'pam' ? '/' : null,
            transport: {
                status: () => request('GET', `${prefix}/status`),
                offer: async params => {
                    const answer = await request('POST', `${prefix}/offer`, params);
                    mediaSession = answer.session;
                    return { type: answer.type, sdp: answer.sdp };
                },
                close: () => {
                    if (!mediaSession) { return; }
                    const id = mediaSession;
                    mediaSession = null;
                    request('DELETE', `${prefix}/console/${id}`, undefined, true).catch(() => {});
                },
            },
        };
        const script = document.createElement('script');
        script.src = '/console.js';
        script.onload = () => main.remove();
        script.onerror = () => { error.textContent = 'Could not load the QSM console. Reload to retry.'; };
        document.head.append(script);
    };
    const refreshGuests = async () => {
        const result = await request('GET', '/api/guests');
        const list = document.querySelector('#guest-list');
        list.replaceChildren();
        document.querySelector('#empty').hidden = result.guests.length > 0;
        for (const guest of result.guests) {
            const item = document.createElement('li');
            const title = document.createElement('span');
            title.textContent = guest.display
                ? `${guest.name} · PID ${guest.pid} · qdev-host display${guest.display.name ? ` ${guest.display.name}` : ''}`
                : `${guest.name} · PID ${guest.pid}`;
            const open = document.createElement('a');
            open.href = `/console?guest=${encodeURIComponent(guest.id)}`;
            open.textContent = 'Open console';
            open.setAttribute('aria-label', `Open console: ${guest.name}`);
            item.append(title, open);
            list.append(item);
        }
    };
    const signedIn = async login => {
        csrf = login.csrf;
        form.hidden = true;
        error.textContent = '';
        document.querySelector('#intro').textContent = `Signed in as ${login.user}`;
        const guest = new URLSearchParams(location.search).get('guest');
        if (location.pathname === '/console' && /^\d+-\d+(?:-\d+-\d+)?$/.test(guest || '')) {
            try { await startConsole(guest); return; }
            catch (failure) { error.textContent = failure.message; }
        }
        guestList.hidden = false;
        await refreshGuests();
    };
    form.addEventListener('submit', async event => {
        event.preventDefault();
        const button = form.querySelector('button');
        button.disabled = true;
        error.textContent = '';
        try {
            if (mode === 'token') {
                token = document.querySelector('#token').value.trim();
                await startConsole(null);
            } else {
                const password = document.querySelector('#password');
                const payload = { username: document.querySelector('#username').value.trim(), password: password.value };
                password.value = '';
                const login = await request('POST', '/api/login', payload);
                await signedIn(login);
            }
        } catch (failure) {
            error.textContent = failure.status === 401
                ? (mode === 'pam' ? 'Invalid username or password.' : 'Invalid access token.') : failure.message;
            token = '';
        } finally { button.disabled = false; }
    });
    document.querySelector('#refresh').addEventListener('click', () => {
        refreshGuests().catch(failure => { error.textContent = failure.message; });
    });
    document.querySelector('#logout').addEventListener('click', async () => {
        try {
            await request('POST', '/api/logout', {});
            csrf = '';
            location.assign('/');
        } catch (failure) { error.textContent = failure.message; }
    });
    (async () => {
        try {
            mode = (await request('GET', '/config')).auth;
            showLogin();
            if (mode === 'pam') {
                try { await signedIn(await request('GET', '/api/session')); }
                catch (failure) { if (failure.status !== 401) { error.textContent = failure.message; } }
            }
        } catch (failure) { error.textContent = failure.message; }
    })();
})();

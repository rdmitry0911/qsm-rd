<!DOCTYPE html>
<html>
<!--
  QSM Direct WebRTC console viewer.  pveproxy renders this template for
  '?console=kvm&webrtc=1&vmid=...&node=...' (pve-manager with WebRTC console
  support); the console attaches through the VM's API call webrtcproxy.
  SPDX-License-Identifier: AGPL-3.0-or-later
-->
<head>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
    <meta http-equiv="X-UA-Compatible" content="IE=edge">
    <title>[% nodename %] - WebRTC Console</title>
    <link rel="stylesheet" type="text/css" href="/pve2/ext6/theme-crisp/resources/theme-crisp-all.css?ver=7.0.0" />
    <link rel="stylesheet" type="text/css" href="/pwt/css/ext6-pmx.css?ver=[% wtversion %]" />
    [%- IF langfile %]
    <script type='text/javascript' src='/pve2/locale/pve-lang-[% lang %].js?v=[% i18n_js_mtime %]'></script>
    [%- ELSE %]
    <script type='text/javascript'>
        function gettext(message) { return message; }
        function ngettext(singular, plural, count) { return count === 1 ? singular : plural; }
    </script>
    [%- END %]
    <script type="text/javascript" src="/pve2/ext6/ext-all.js?ver=7.0.0"></script>
    <!-- proxmoxlib.js expects the ExtJS charts package -->
    <script type="text/javascript" src="/pve2/ext6/charts.js?ver=7.0.0"></script>
    <script type="text/javascript">
    Proxmox = {
        Setup: { auth_cookie_name: 'PVEAuthCookie' },
        defaultLang: '[% lang %]',
        NodeName: '[% nodename %]',
        UserName: '[% username %]',
        CSRFPreventionToken: '[% token %]',
    };
    (function () {
        const query = new URLSearchParams(window.location.search);
        const vmid = Number(query.get('vmid'));
        const node = query.get('node') || '';
        if (Number.isSafeInteger(vmid) && vmid >= 100 && /^[A-Za-z0-9.-]{1,63}$/.test(node)) {
            window.QSM_WEBRTC_VIEWER = { vmid, node, csrf: Proxmox.CSRFPreventionToken };
            if (query.get('vmname')) {
                document.title = `VM ${vmid} (${query.get('vmname')}) - WebRTC Console`;
            }
        }
    }());
    </script>
    <script type="text/javascript" src="/proxmoxlib.js?ver=[% wtversion %]"></script>
    <script type="text/javascript" src="/webrtc/qsm-direct-console.js?ver=[% wtversion %]"></script>
</head>
<body style="margin:0;background:#000"></body>
</html>

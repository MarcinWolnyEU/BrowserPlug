// Inbound-reachability self-diagnosis, isolated from options.js on purpose.
//
// options.js dispatches custom DOM events at its existing state transitions and this file
// owns everything else (the fetch, the DOM it renders into, the wording). That event set is
// the ONLY contract between the two:
//   'browserplug:wan-unreachable'    check-host.net reported (or timed out on) the server
//   'browserplug:wan-check-reset'    a fresh WAN check started, or one succeeded
//   'browserplug:lan-firewall-check' the LAN panel is showing; ask whether the firewall
//                                    will actually admit a machine on the LAN
//
// Both panels get the same answer from the server's GET /diagnose-wan (server/
// wan_diagnosis.py): is the port really listening (OS-level), is the bind wide enough, and
// will Windows Firewall admit the port on the network this machine is CURRENTLY on. The WAN
// panel additionally gets the router port-forward advice, which is meaningless on a LAN.
//
// The LAN half exists because nothing else in this page can see that failure. A firewall
// rule scoped to the Private profile is inert while Windows has the active Wi-Fi filed as
// Public, and the symptom is silence: the SYN is dropped, so the server logs nothing, the
// "Test connection" button still passes (it only ever dials localhost), and the remote
// browser reports Disconnected, which is also what a wrong token looks like.

(function () {
    'use strict';

    const DEFAULT_MCP_PORT = 2830;

    // Re-running on every render would put a PowerShell round-trip behind the health poll.
    const RERUN_AFTER_MS = 15000;

    const PANELS = {
        wan: { boxId: 'wanDiagnosis', anchorId: 'wanFirewallPort', router: true },
        lan: { boxId: 'lanDiagnosis', anchorId: 'lanFirewallPort', router: false }
    };

    function diagnosisBox(panel) {
        const spec = PANELS[panel];
        let box = document.getElementById(spec.boxId);
        if (box) return box;
        // Anchored right after that panel's "Allow inbound TCP on port ..." paragraph, in
        // the "This machine (runs the server)" column; no markup needs adding to
        // options.html.
        const portCode = document.getElementById(spec.anchorId);
        const anchor = portCode ? portCode.closest('p') : null;
        box = document.createElement('div');
        box.id = spec.boxId;
        box.className = 'diagnosis';
        box.hidden = true;
        if (anchor && anchor.parentNode) {
            anchor.parentNode.insertBefore(box, anchor.nextSibling);
        } else {
            // Defensive fallback; should not happen on the real options page.
            document.body.appendChild(box);
        }
        return box;
    }

    function localMcpUrl(path) {
        const host = (document.getElementById('hostname')?.value || '').trim() || 'localhost';
        const rawPort = document.getElementById('mcpPort')?.value;
        const parsed = parseInt(rawPort, 10);
        const port = (!isNaN(parsed) && parsed >= 1 && parsed <= 65535) ? parsed : DEFAULT_MCP_PORT;
        return `http://${host}:${port}${path}`;
    }

    function appendLine(parent, text, cls) {
        const div = document.createElement('div');
        if (cls) div.className = cls;
        div.textContent = text;
        parent.appendChild(div);
    }

    // The fix is a command the user has to run elevated, so it is rendered as something to
    // copy rather than described in prose. textContent throughout: a rule's display name
    // comes off the user's own machine and is not ours to trust as markup.
    function appendFix(parent, command) {
        if (!command) return;
        appendLine(parent, 'Fix, in PowerShell as Administrator:');
        const block = document.createElement('div');
        block.className = 'code-block';
        const code = document.createElement('code');
        code.textContent = command;
        block.appendChild(code);
        parent.appendChild(block);
    }

    function appendFirewall(box, data) {
        const fw = data.firewall || {};
        if (!fw.checked) {
            appendLine(box, fw.detail || 'Windows Firewall status could not be checked.');
            return;
        }
        // Both verdicts print the server's own sentence: it already names every port it
        // checked and the network it judged them against, so the page adds a tick or a
        // cross and nothing else. Composing a second sentence here is how the two halves
        // drift apart.
        if (fw.ok === true) {
            appendLine(box, `✓ ${fw.detail}`, 'ok-line');
            appendLine(box, 'If you also run third-party antivirus/security software, check '
                + 'its firewall too, since it can block connections independently of '
                + 'Windows Firewall.');
            return;
        }
        if (fw.ok === null) {
            appendLine(box, fw.detail || 'Windows Firewall state is inconclusive.');
            return;
        }
        appendLine(box, `✗ ${fw.detail}`, 'err-line');
        appendFix(box, fw.fix_command);
    }

    function renderDiagnosis(box, data, panel) {
        box.replaceChildren();

        if (data.listening.ok === false) {
            appendLine(box, `✗ Server is NOT listening on port ${data.port}. Restart it.`,
                'err-line');
            box.hidden = false;
            return;
        }

        appendLine(box,
            data.listening.ok
                ? `✓ Server is listening (${data.listening.detail}).`
                : `Server listening state could not be verified (${data.listening.detail}).`,
            data.listening.ok ? 'ok-line' : null);

        if (data.host_bind && data.host_bind.ok === false) {
            appendLine(box, `✗ Bound to ${data.host_bind.host}, not all interfaces. `
                + 'Click Set & Restart button above, then refresh, or switch the bind address to 0.0.0.0 (Advanced, below).', 'err-line');
            box.hidden = false;
            return;
        }

        appendFirewall(box, data);

        if (PANELS[panel].router && data.listening.ok !== false
                && (!data.host_bind || data.host_bind.ok !== false) && data.gateway_ip) {
            const p = document.createElement('div');
            p.appendChild(document.createTextNode('Likely cause: your router ('));
            const a = document.createElement('a');
            a.href = `http://${data.gateway_ip}`;
            a.target = '_blank';
            a.rel = 'noreferrer';
            a.textContent = `http://${data.gateway_ip}`;
            p.appendChild(a);
            p.appendChild(document.createTextNode(
                `) is missing a port-forwarding rule. Add: external port ${data.port} TCP `
                + `→ internal IP ${data.lan_ip || '?'} : internal port ${data.port} `
                + '(look under NAT Forwarding, Virtual Servers, or Port Forwarding). '
                + 'Search online or ask an LLM agent for help with your specific router model.'));
            box.appendChild(p);
        }

        box.hidden = false;
    }

    // Per panel, so a WAN reset cannot cancel a LAN run mid-flight.
    const generation = { wan: 0, lan: 0 };
    const lastRun = { wan: 0, lan: 0 };

    async function runDiagnosis(panel, force) {
        const now = Date.now();
        if (!force && now - lastRun[panel] < RERUN_AFTER_MS) return;
        lastRun[panel] = now;
        const gen = ++generation[panel];
        const box = diagnosisBox(panel);
        let data = null;
        try {
            // Signed with the token: this endpoint follows the LAN bind now, so a remote
            // browser reaches it and must authenticate (server/ws/http_auth.py), and the
            // token itself must not cross the network (ws-crypto.js signRequest).
            const token = (document.getElementById('connectionToken')?.value || '').trim();
            const wsCrypto = globalThis.BrowserplugWsCrypto;
            const headers = (token && wsCrypto)
                ? { [wsCrypto.AUTH_HEADER]: await wsCrypto.signRequest(token, 'GET', localMcpUrl('/diagnose-wan')) }
                : {};
            const res = await fetch(localMcpUrl('/diagnose-wan'), {
                method: 'GET', cache: 'no-store', headers: headers
            });
            if (res.ok) data = await res.json();
        } catch (e) {
            // The local server is unreachable too (or mid-restart); nothing more to add
            // beyond what the reachability indicator already says.
        }
        if (gen !== generation[panel]) return;   // superseded by a reset/another run
        if (!data) { box.hidden = true; return; }
        renderDiagnosis(box, data, panel);
    }

    function reset(panel) {
        generation[panel]++;   // supersedes any fetch still in flight
        lastRun[panel] = 0;
        const box = document.getElementById(PANELS[panel].boxId);
        if (box) box.hidden = true;
    }

    document.addEventListener('browserplug:wan-unreachable', () => { runDiagnosis('wan', true); });
    document.addEventListener('browserplug:wan-check-reset', () => reset('wan'));
    document.addEventListener('browserplug:lan-firewall-check', (e) => {
        runDiagnosis('lan', !!(e.detail && e.detail.force));
    });
})();

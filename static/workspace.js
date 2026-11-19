// Reporting actions share a run-specific mode, retained while moving between tabs.
document.addEventListener('DOMContentLoaded', () => {
    initializeVisualSystem();
    const panel = document.querySelector('[data-report-run]');
    if (panel) initializeReporting(panel);
    document.querySelectorAll('label:not([for])').forEach(label => {
        const field = label.parentElement.querySelector('input[id],select[id],textarea[id]');
        if (field) label.htmlFor = field.id;
    });
    document.querySelectorAll('.modal').forEach(modal => {
        modal.setAttribute('role', 'dialog');
        modal.setAttribute('aria-modal', 'true');
        const heading = modal.querySelector('h2,h3');
        if (heading) {
            heading.id = heading.id || modal.id + '-title';
            modal.setAttribute('aria-labelledby', heading.id);
        }
    });
});

function initializeReporting(panel) {
    const runId = panel.dataset.reportRun;
    const modeInputs = Array.from(panel.querySelectorAll('input[name="report-mode"]'));
    const selectedMode = () => modeInputs.find(input => input.checked).value;
    const modeLabel = mode => mode === 'full' ? 'Full Technical' : 'Standard';
    const storageKey = 'qa-report-mode-' + runId;
    const validModes = ['standard', 'full'];
    let stored;
    try { stored = sessionStorage.getItem(storageKey); } catch (error) { /* Storage may be disabled. */ }
    const requested = new URLSearchParams(location.search).get('mode');
    const initialMode = validModes.includes(requested) ? requested : (validModes.includes(stored) ? stored : 'standard');
    modeInputs.forEach(input => input.checked = input.value === initialMode);

    const updateMode = () => {
        try { sessionStorage.setItem(storageKey, selectedMode()); } catch (error) { /* Actions still work without storage. */ }
        panel.querySelector('[data-mode-description]').textContent = selectedMode() === 'full'
            ? 'Full Technical includes every recorded check, diagnostics and the historical non-customer archive.'
            : 'Standard explains the outcome, customer findings, review needs and coverage, with a compact technical summary.';
        panel.querySelectorAll('[data-report-link]').forEach(link => {
            link.href = link.dataset.reportLink + '?mode=' + selectedMode();
        });
        panel.querySelector('[data-preview-mode]').textContent = modeLabel(selectedMode());
        const frame = panel.querySelector('iframe');
        if (!frame.hidden) frame.src = '/run/' + runId + '/preview?mode=' + selectedMode();
    };
    modeInputs.forEach(input => input.addEventListener('change', updateMode));
    updateMode();
    panel.querySelector('[data-preview]').addEventListener('click', () => {
        const frame = panel.querySelector('iframe');
        panel.querySelector('[data-preview-shell]').hidden = false;
        frame.hidden = false;
        frame.src = '/run/' + runId + '/preview?mode=' + selectedMode();
    });
    panel.querySelector('[data-copy]').addEventListener('click', async () => {
        const restore = beginAction(panel.querySelector('[data-copy]'), 'Copying…');
        const full = selectedMode() === 'full';
        const text = document.getElementById(full ? 'run-report-full-text' : 'run-report-text').textContent;
        const html = document.getElementById(full ? 'run-report-full-html' : 'run-report-html').innerHTML;
        try {
            try {
                await navigator.clipboard.write([new ClipboardItem({
                    'text/plain': new Blob([text], {type: 'text/plain'}),
                    'text/html': new Blob([html], {type: 'text/html'})
                })]);
            } catch (error) {
                await navigator.clipboard.writeText(text);
            }
            showToast('Report copied · ' + modeLabel(selectedMode()));
        } catch (error) {
            showToast('Clipboard unavailable. Export TXT to obtain the same report.');
        } finally {
            restore();
        }
    });
    panel.querySelectorAll('[data-export]').forEach(button => {
        button.addEventListener('click', async () => {
            const restore = beginAction(button, 'Exporting…');
            const mode = selectedMode();
            try {
                const response = await fetch('/api/export/' + runId + '/' + button.dataset.export + '?mode=' + mode, {method: 'POST'});
                const data = await response.json();
                if (!response.ok || !data.filename) throw Error(data.error || data.message || 'Export failed');
                showToast('Created ' + data.filename);
                const output = panel.querySelector('[data-export-result]');
                output.hidden = false;
                output.querySelector('span').textContent = modeLabel(mode) + ' · ' + data.filename;
                output.querySelector('button').onclick = () => openLocal('/api/exports/' + encodeURIComponent(data.filename) + '/open');
            } catch (error) {
                showToast(error.message);
            } finally {
                restore();
            }
        });
    });
    panel.querySelector('[data-folder]').addEventListener('click', () => openLocal('/api/open-exports-folder'));
}

async function openLocal(url) {
    try {
        const response = await fetch(url, {method: 'POST'});
        const data = await response.json();
        if (!response.ok || data.status !== 'success') throw Error(data.error || data.message || 'Could not open in Windows');
        showToast('Opened in Windows');
    } catch (error) {
        showToast(error.message);
    }
}

function statusTone(value) {
    const status = value.trim().toUpperCase().replace(/^EXECUTION:\s*|^SESSION\s+/, '');
    if (['COMPLETED','PASS','APPROVED','FIXED','CLOSED','READY'].includes(status)) return 'success';
    if (['FAIL','FAILED','REJECTED','CRITICAL'].includes(status)) return 'danger';
    if (['PENDING','PENDING REVIEW','NOT REVIEWED YET','REVIEW','RETEST','RETEST REQUIRED','NEEDS REVIEW','LIMITED COVERAGE','OBSERVATION','SAFETY ACTION','AUTH REQUIRED','ABORTED','INTERRUPTED'].includes(status)) return 'warning';
    if (['DIAGNOSTIC','REDIRECTED','RUNNING','STARTING'].includes(status)) return 'info';
    if (['OPEN','BROWSER CLOSED','CHECKING SESSION…','CHECKING SESSION...'].includes(status)) return 'neutral';
    return null;
}

function updateSelectTone(select) {
    const value = select.value.toUpperCase();
    let tone = 'info';
    if (['f_sev','f_risk'].includes(select.id)) {
        tone = {LOW:'success',MEDIUM:'warning',HIGH:'danger',CRITICAL:'danger'}[value] || 'info';
    } else if (select.id === 'f_conf') {
        tone = {HIGH:'success',MEDIUM:'warning',LOW:'warning'}[value] || 'info';
    } else if (select.id === 'f_status' || select.dataset.semantic === 'review') {
        tone = statusTone(value) || 'neutral';
    }
    select.dataset.tone = tone;
}

function beginAction(button, label) {
    if (!button) return () => {};
    const original = button.innerHTML;
    const disabled = button.disabled;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.textContent = label;
    return () => { button.innerHTML = original; button.disabled = disabled; button.removeAttribute('aria-busy'); };
}

function queueFeedback(message) {
    try { sessionStorage.setItem('qa-action-feedback', message); } catch (error) { showToast(message); }
}

function initializeVisualSystem() {
    document.querySelectorAll('select').forEach(select => {
        updateSelectTone(select);
        select.addEventListener('change', () => updateSelectTone(select));
    });
    // Decorative icons share one stroke system; accessible names remain the visible text.
    const paths = {
        edit:'M3 11l8-8 2 2-8 8-3 1z',
        copy:'M6 5h7v9H6z M3 11V2h7',
        export:'M8 2v8 M5 7l3 3 3-3 M3 11v3h10v-3',
        delete:'M3 4h10 M6 2h4 M5 4v10h6V4 M7 6v6 M9 6v6',
        save:'M3 2h9l2 2v10H2V2z M5 2v4h6V2 M5 10h6v4H5z',
        review:'M4 2h8v12H4z M6 8l2 2 3-4',
        open:'M8 3h5v5 M13 3L7 9 M6 3H3v10h10v-3'
    };
    const refresh = () => {
        document.querySelectorAll('.badge').forEach(badge => {
            const tone = statusTone(badge.textContent);
            if (tone && badge.dataset.tone !== tone) badge.dataset.tone = tone;
        });
        document.querySelectorAll('.btn').forEach(button => {
            if (button.querySelector('.action-icon') || button.hasAttribute('aria-busy')) return;
            const text = button.textContent.trim();
            const key = /^(Edit)/i.test(text) ? 'edit' : /^Copy/i.test(text) ? 'copy' : /^(Word|TXT|Markdown|HTML)$/.test(text) ? 'export' : /^(Delete|Permanently delete|Reject)/i.test(text) ? 'delete' : /^Save/i.test(text) ? 'save' : /^(Review|Approve)/i.test(text) ? 'review' : /^Open/i.test(text) ? 'open' : null;
            if (!key) return;
            const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
            svg.setAttribute('viewBox','0 0 16 16'); svg.setAttribute('class','action-icon'); svg.setAttribute('aria-hidden','true'); svg.setAttribute('focusable','false');
            const path = document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d',paths[key]);svg.append(path);button.prepend(svg);
        });
    };
    refresh();
    new MutationObserver(refresh).observe(document.querySelector('main'), {childList:true,subtree:true});
    try {
        const message = sessionStorage.getItem('qa-action-feedback');
        sessionStorage.removeItem('qa-action-feedback');
        if (message) showToast(message);
    } catch (error) { /* Feedback still works without session storage on the current page. */ }
    // Wrap only UI actions; each original function retains its payload and error handling.
    ['saveEdits','updateReviewStatus','submitEditProject','submitDeleteProject','createProject','toggleInteractive','sessionAction','openLocal','openNativeScreenshot'].forEach(name => {
        const original = window[name];
        if (typeof original !== 'function') return;
        let pending = false;
        window[name] = async function(...args) {
            if (pending) return;
            pending = true;
            const event = window.event;
            const button = event?.submitter || event?.target?.closest('button');
            const restore = beginAction(button, name.toLowerCase().includes('delete') ? 'Deleting…' : name === 'updateReviewStatus' ? 'Updating review…' : name.toLowerCase().includes('project') || name === 'saveEdits' ? 'Saving…' : 'Working…');
            try { return await original.apply(this,args); } finally { restore(); pending = false; }
        };
    });
}

let dialogReturnFocus;
function openDialog(id) {
    dialogReturnFocus = document.activeElement;
    const modal = document.getElementById(id);
    modal.style.display = 'flex';
    (modal.querySelector('input,select,textarea') || modal.querySelector('button'))?.focus();
}
function closeDialog(id) {
    document.getElementById(id).style.display = 'none';
    dialogReturnFocus?.focus();
}

document.addEventListener('keydown', event => {
    const modal = Array.from(document.querySelectorAll('.modal')).find(el => el.style.display === 'flex');
    if (!modal) return;
    if (event.key === 'Escape') {
        event.preventDefault();
        closeDialog(modal.id);
    }
    if (event.key === 'Tab') {
        const fields = Array.from(modal.querySelectorAll('a,button,input,select,textarea,[tabindex="0"]'))
            .filter(el => !el.disabled && el.getClientRects().length);
        if (!fields.length) return;
        const first = fields[0], last = fields[fields.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }
});

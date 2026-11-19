"""Controlled localhost profile test server fixture.

Provides an authenticated profile page containing:
1. Initially visible controls:
   - Safe tabs ('Personal Info', 'Security & Privacy').
   - Safe disclosures / accordions ('Employment Details').
   - Safe modal dialog ('View Employment History').
   - Safe local form validation ('Validate Work History Dates').
   - Broken controls:
     * Inert disclosure control (aria-expanded does not change, content does not change).
     * Dangling aria-controls (target ID does not exist in DOM).
     * Unrevealed content (aria-expanded changes to true, but content remains hidden).
     * Crashing control (throws uncaught TypeError on click).
   - Mutating and prohibited server actions:
     * 'Save Profile Changes' (type=submit).
     * 'Upload Avatar Image'.
     * 'Delete Account Permanently'.
   - Unsupported input controls:
     * 'Username' text input.
     * 'Profile Notes' text input.
2. Reachable controls revealed behind safe tabs:
   - Inside 'Security & Privacy':
     * 'Two-Factor Authentication Setup' accordion (working safe disclosure).
     * 'Revoke All Security Keys' button (prohibited destructive action).
     * 'Recovery Phone' text input (unsupported input control).
"""

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json
import threading
import urllib.parse


PROFILE_INVENTORY = [
    {
        "inventory_id": "tab-personal",
        "category": "initially_visible_safe_tab",
        "role": "tab",
        "name": "Personal Info",
        "selector": "#tab-personal",
        "expected_verdict": "LIMITED COVERAGE",
        "expected_defect_category": None,
        "notes": "Initially selected tab; clicking already-selected tab produces no state transition (inconclusive / limited coverage, zero false defect).",
    },
    {
        "inventory_id": "tab-security",
        "category": "initially_visible_safe_tab",
        "role": "tab",
        "name": "Security & Privacy",
        "selector": "#tab-security",
        "expected_verdict": "PASS",
        "expected_defect_category": None,
        "notes": "Switches to security panel, revealing nested 2FA accordion and keys.",
    },
    {
        "inventory_id": "tab-preferences",
        "category": "initially_visible_safe_tab",
        "role": "tab",
        "name": "Preferences",
        "selector": "#tab-preferences",
        "expected_verdict": "PASS",
        "expected_defect_category": None,
        "notes": "Switches to preferences panel, revealing delayed transition disclosure.",
    },
    {
        "inventory_id": "acc-employment",
        "category": "initially_visible_safe_disclosure",
        "role": "button",
        "name": "View Employment Details",
        "selector": "#acc-employment",
        "expected_verdict": "PASS",
        "expected_defect_category": None,
        "notes": "Expands employment details accordion with positive visibility change.",
    },
    {
        "inventory_id": "btn-view-history-dialog",
        "category": "safe_modal_dialog",
        "role": "button",
        "name": "View Employment History",
        "selector": "#btn-view-history-dialog",
        "expected_verdict": "PASS",
        "expected_defect_category": None,
        "notes": "Opens non-mutating modal dialog (#dialog-employment-history) with verified active visibility and close control.",
    },
    {
        "inventory_id": "btn-close-history-dialog",
        "category": "revealed_safe_dialog_dismissal",
        "role": "button",
        "name": "Close History",
        "selector": "#btn-close-history-dialog",
        "expected_verdict": "PASS",
        "expected_defect_category": None,
        "notes": "Reachable inside modal dialog; dismisses dialog without page reload or mutation.",
    },
    {
        "inventory_id": "btn-validate-history",
        "category": "safe_local_validation",
        "role": "button",
        "name": "Validate Work History Dates",
        "selector": "#btn-validate-history",
        "expected_verdict": "PASS",
        "expected_defect_category": None,
        "notes": "Validates date constraints locally (start date > end date) via contract without server mutation.",
    },
    {
        "inventory_id": "btn-actions-menu",
        "category": "initially_visible_safe_menu",
        "role": "button",
        "name": "Quick Actions",
        "selector": "#btn-actions-menu",
        "expected_verdict": "PASS",
        "expected_defect_category": None,
        "notes": "Expands Quick Actions dropdown menu (#menu-actions) revealing nested actions.",
    },
    {
        "inventory_id": "menu-item-audit",
        "category": "revealed_safe_menu_item",
        "role": "menuitem",
        "name": "Audit Trail Record",
        "selector": "#menu-item-audit",
        "expected_verdict": "PASS",
        "expected_defect_category": None,
        "notes": "Reachable inside Quick Actions menu; toggles audit panel visibility.",
    },
    {
        "inventory_id": "menu-item-broken",
        "category": "revealed_broken_menu_item",
        "role": "menuitem",
        "name": "Broken Export Options",
        "selector": "#menu-item-broken",
        "expected_verdict": "FAIL",
        "expected_defect_category": "DANGLING_ARIA_CONTROLS",
        "notes": "Reachable inside Quick Actions menu; aria-controls points to nonexistent DOM element.",
    },
    {
        "inventory_id": "menu-item-inert",
        "category": "revealed_inert_menu_item",
        "role": "menuitem",
        "name": "Inert Help Action",
        "selector": "#menu-item-inert",
        "expected_verdict": "LIMITED COVERAGE",
        "expected_defect_category": None,
        "notes": "Reachable inside Quick Actions menu; produces no DOM change upon click.",
    },
    {
        "inventory_id": "acc-2fa",
        "category": "revealed_safe_disclosure",
        "role": "button",
        "name": "Two-Factor Authentication Setup",
        "selector": "#acc-2fa",
        "expected_verdict": "PASS",
        "expected_defect_category": None,
        "notes": "Reachable only behind Security tab; replayed and verified via disposable sequence.",
    },
    {
        "inventory_id": "acc-delayed",
        "category": "revealed_safe_disclosure",
        "role": "button",
        "name": "Delayed Transition Details",
        "selector": "#acc-delayed",
        "expected_verdict": "PASS",
        "expected_defect_category": None,
        "notes": "Reachable behind Preferences tab; reveals panel-delayed after 150ms delay.",
    },
    {
        "inventory_id": "btn-dangling",
        "category": "deliberately_broken_dangling",
        "role": "button",
        "name": "Dangling Target Button",
        "selector": "#btn-dangling",
        "expected_verdict": "FAIL",
        "expected_defect_category": "DANGLING_ARIA_CONTROLS",
        "notes": "aria-controls points to nonexistent DOM element.",
    },
    {
        "inventory_id": "btn-unrevealed",
        "category": "deliberately_broken_unrevealed",
        "role": "button",
        "name": "Unrevealed Content Button",
        "selector": "#btn-unrevealed",
        "expected_verdict": "FAIL",
        "expected_defect_category": "CONTENT_NOT_REVEALED",
        "notes": "aria-expanded toggles true, but target panel remains hidden.",
    },
    {
        "inventory_id": "btn-crash",
        "category": "deliberately_broken_crash",
        "role": "button",
        "name": "Crash On Click Button",
        "selector": "#btn-crash",
        "expected_verdict": "FAIL",
        "expected_defect_category": "PAGE_SCRIPT_ERROR",
        "notes": "Click handler throws uncaught TypeError.",
    },
    {
        "inventory_id": "btn-inert",
        "category": "deliberately_broken_inert",
        "role": "button",
        "name": "Inert Disclosure Button",
        "selector": "#btn-inert",
        "expected_verdict": "LIMITED COVERAGE",
        "expected_defect_category": None,
        "notes": "Control receives click but produces no DOM change; inconclusive, zero false customer defects.",
    },
    {
        "inventory_id": "btn-save",
        "category": "mutating_server_action",
        "role": "button",
        "name": "Save Profile Changes",
        "selector": "#btn-save",
        "expected_verdict": "LIMITED COVERAGE",
        "expected_defect_category": None,
        "notes": "Submit button requiring server state mutation (Phase D scope).",
    },
    {
        "inventory_id": "btn-upload",
        "category": "mutating_server_action",
        "role": "button",
        "name": "Upload Avatar Image",
        "selector": "#btn-upload",
        "expected_verdict": "LIMITED COVERAGE",
        "expected_defect_category": None,
        "notes": "Mutating upload action requiring server modification (Phase D scope).",
    },
    {
        "inventory_id": "btn-delete",
        "category": "prohibited_destructive_action",
        "role": "button",
        "name": "Delete Account Permanently",
        "selector": "#btn-delete",
        "expected_verdict": "LIMITED COVERAGE",
        "expected_defect_category": None,
        "notes": "Prohibited destructive action outside authorized execution scope (Phase D permit required).",
    },
    {
        "inventory_id": "btn-delete-keys",
        "category": "revealed_prohibited_action",
        "role": "button",
        "name": "Revoke All Security Keys",
        "selector": "#btn-delete-keys",
        "expected_verdict": "LIMITED COVERAGE",
        "expected_defect_category": None,
        "notes": "Prohibited destructive action revealed behind Security tab.",
    },
    {
        "inventory_id": "pref-username",
        "category": "unsupported_input_control",
        "role": "textbox",
        "name": "Username",
        "selector": "#pref-username",
        "expected_verdict": "LIMITED COVERAGE",
        "expected_defect_category": None,
        "notes": "Initially visible text input unsupported for ephemeral disclosure.",
    },
    {
        "inventory_id": "pref-notes",
        "category": "unsupported_input_control",
        "role": "textbox",
        "name": "Profile Notes",
        "selector": "#pref-notes",
        "expected_verdict": "LIMITED COVERAGE",
        "expected_defect_category": None,
        "notes": "Uncontracted domain text input requiring data contract (Phase D scope).",
    },
    {
        "inventory_id": "sec-phone",
        "category": "revealed_unsupported_input",
        "role": "textbox",
        "name": "Recovery Phone",
        "selector": "#sec-phone",
        "expected_verdict": "LIMITED COVERAGE",
        "expected_defect_category": None,
        "notes": "Revealed text input behind Security tab unsupported for ephemeral disclosure.",
    },
    {
        "inventory_id": "input-start-date",
        "category": "unsupported_input_control",
        "role": "textbox",
        "name": "Start Date",
        "selector": "#input-start-date",
        "expected_verdict": "LIMITED COVERAGE",
        "expected_defect_category": None,
        "notes": "Editable date input used by domain validation contract.",
    },
    {
        "inventory_id": "input-end-date",
        "category": "unsupported_input_control",
        "role": "textbox",
        "name": "End Date",
        "selector": "#input-end-date",
        "expected_verdict": "LIMITED COVERAGE",
        "expected_defect_category": None,
        "notes": "Editable date input used by domain validation contract.",
    },
]


PROFILE_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="user-id" content="tester@example.com">
  <meta name="account-id" content="tester@example.com">
  <meta name="authenticated-user" content="tester@example.com">
  <title>User Profile - example.com simulation</title>
  <style>
    body { font-family: sans-serif; margin: 24px; color: #222; }
    .tabs { display: flex; border-bottom: 2px solid #ddd; margin-bottom: 16px; }
    .tab { padding: 8px 16px; border: none; background: none; font-size: 15px; cursor: pointer; border-bottom: 2px solid transparent; }
    .tab[aria-selected="true"] { border-bottom-color: #12665f; font-weight: bold; color: #12665f; }
    .tab-panel { padding: 12px 0; }
    .accordion-btn { padding: 8px 12px; margin: 8px 0; display: block; cursor: pointer; }
    .accordion-panel { padding: 8px 12px; background: #f9f9f9; border-left: 3px solid #12665f; margin-bottom: 8px; }
    .mutating-section { margin-top: 24px; padding: 16px; background: #fff3f3; border: 1px solid #ffcccc; border-radius: 4px; }
    .danger-btn { background: #d32f2f; color: white; border: none; padding: 8px 16px; cursor: pointer; border-radius: 4px; }
    .primary-btn { background: #12665f; color: white; border: none; padding: 8px 16px; cursor: pointer; border-radius: 4px; }
    dialog::backdrop { background: rgba(0, 0, 0, 0.4); }
    [hidden] { display: none !important; }
  </style>
</head>
<body>
  <div id="auth-session" data-authenticated-user="tester@example.com" data-user-id="tester@example.com" style="display:none;">tester@example.com</div>
  <h1>User Profile: Tester</h1>
  <p>Authenticated Session: active (example.com simulation)</p>

  <!-- Navigation / Tablist -->
  <div role="tablist" class="tabs" aria-label="Profile Sections">
    <button role="tab" id="tab-personal" class="tab" aria-selected="true" aria-controls="panel-personal">Personal Info</button>
    <button role="tab" id="tab-security" class="tab" aria-selected="false" aria-controls="panel-security">Security &amp; Privacy</button>
    <button role="tab" id="tab-preferences" class="tab" aria-selected="false" aria-controls="panel-preferences">Preferences</button>
  </div>

  <!-- Panels -->
  <div role="tabpanel" id="panel-personal" class="tab-panel">
    <h2>Personal Information</h2>
    <p>Name: Tester | Role: Senior QA Engineer</p>
    <p><label for="pref-username">Username</label>: <input type="text" id="pref-username" name="username" value="Tester"></p>
    <p><label for="pref-notes">Profile Notes</label>: <input type="text" id="pref-notes" name="notes" value="Senior QA Staff"></p>
    
    <button class="accordion-btn" id="acc-employment" aria-expanded="false" aria-controls="panel-employment">View Employment Details</button>
    <div id="panel-employment" class="accordion-panel" hidden>
      <p>Company: QA Engineering Nordic | Department: Quality Verification</p>
    </div>

    <!-- Safe Modal Dialog Trigger and Dialog -->
    <div style="margin: 14px 0;">
      <button type="button" class="primary-btn" id="btn-view-history-dialog" aria-haspopup="dialog" aria-controls="dialog-employment-history">View Employment History</button>
      <dialog id="dialog-employment-history" style="padding: 20px; border: 2px solid #12665f; border-radius: 8px; background: white; max-width: 450px;">
        <h3 style="margin-top:0;">Employment History Record</h3>
        <p>Position: Senior Verification Specialist (2020 - Present)</p>
        <p>Department: Quality Engineering Governance</p>
        <button type="button" id="btn-close-history-dialog" class="danger-btn" style="margin-top: 10px; cursor: pointer;">Close History</button>
      </dialog>
    </div>

    <!-- Safe Local Form Date Validation Section -->
    <div id="section-work-history" style="margin: 16px 0; padding: 14px; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px;">
      <h3 style="margin-top:0;">Work History Date Verification</h3>
      <p>Verify consistency of entered employment periods locally before submission.</p>
      <div style="display: flex; gap: 12px; align-items: center; margin-bottom: 10px;">
        <label for="input-start-date">Start Date: </label><input type="date" id="input-start-date" value="2024-05-01">
        <label for="input-end-date">End Date: </label><input type="date" id="input-end-date" value="2022-01-01">
      </div>
      <button type="button" id="btn-validate-history" class="primary-btn" aria-controls="history-date-feedback">Validate Work History Dates</button>
      <div id="history-date-feedback" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;" hidden></div>
    </div>

    <!-- Quick Actions Menu (Dropdown) -->
    <div style="margin: 14px 0; position: relative;">
      <button type="button" id="btn-actions-menu" class="primary-btn" aria-haspopup="menu" aria-expanded="false" aria-controls="menu-actions">Quick Actions</button>
      <div id="menu-actions" role="menu" class="accordion-panel" hidden style="margin-top: 6px;">
        <button type="button" role="menuitem" id="menu-item-audit" class="accordion-btn" aria-controls="panel-audit">Audit Trail Record</button>
        <div id="panel-audit" hidden><p>Audit log entries verified.</p></div>
        <button type="button" role="menuitem" id="menu-item-broken" class="accordion-btn" aria-controls="nonexistent-export-target">Broken Export Options</button>
        <button type="button" role="menuitem" id="menu-item-inert" class="accordion-btn">Inert Help Action</button>
      </div>
    </div>

    <!-- EXPERIENCE_MUTATION_SECTION_PLACEHOLDER -->
  </div>

  <div role="tabpanel" id="panel-security" class="tab-panel" hidden>
    <h2>Security Settings</h2>
    <p>Password last changed: 30 days ago</p>
    <button class="accordion-btn" id="acc-2fa" aria-expanded="false" aria-controls="panel-2fa">Two-Factor Authentication Setup</button>
    <div id="panel-2fa" class="accordion-panel" hidden>
      <p>2FA is currently active via Authenticator App.</p>
    </div>
    <div style="margin-top: 12px;">
      <button class="danger-btn" id="btn-delete-keys">Revoke All Security Keys</button>
    </div>
    <p style="margin-top: 12px;"><label for="sec-phone">Recovery Phone</label>: <input type="text" id="sec-phone" name="phone" value="+46701234567"></p>
  </div>

  <div role="tabpanel" id="panel-preferences" class="tab-panel" hidden>
    <h2>User Preferences</h2>
    <p>Configure interface and asynchronous workflow options.</p>
    <button class="accordion-btn" id="acc-delayed" aria-expanded="false" aria-controls="panel-delayed">Delayed Transition Details</button>
    <div id="panel-delayed" class="accordion-panel" hidden>
      <p>Asynchronously revealed preference details settled after 150ms.</p>
    </div>
  </div>

  <!-- Deliberately Broken Controls Section -->
  <div style="margin-top:24px; padding:16px; background:#f4f6f8; border-radius:4px;">
    <h2>Interactive Verification Checks</h2>
    
    <!-- 1. Inert Control: aria-expanded does not change, content does not change -->
    <button class="accordion-btn" id="btn-inert" aria-expanded="false" aria-controls="panel-inert">Inert Disclosure Button</button>
    <div id="panel-inert" class="accordion-panel" hidden>
      <p>This content should never be revealed because the button is inert.</p>
    </div>

    <!-- 2. Dangling Target: aria-controls points to nonexistent ID -->
    <button class="accordion-btn" id="btn-dangling" aria-expanded="false" aria-controls="nonexistent-target-element">Dangling Target Button</button>

    <!-- 3. Unrevealed Content: aria-expanded toggles true, but content remains hidden -->
    <button class="accordion-btn" id="btn-unrevealed" aria-expanded="false" aria-controls="panel-unrevealed">Unrevealed Content Button</button>
    <div id="panel-unrevealed" class="accordion-panel" hidden>
      <p>This panel stays hidden even when expanded is toggled.</p>
    </div>

    <!-- 4. Crashing Button: Throws runtime TypeError -->
    <button class="accordion-btn" id="btn-crash" aria-expanded="false" aria-controls="panel-crash">Crash On Click Button</button>
    <div id="panel-crash" class="accordion-panel" hidden>
      <p>Content for crashing button.</p>
    </div>
  </div>

  <!-- Potentially Mutating Server Actions (Phase D scope) -->
  <div class="mutating-section">
    <h2>Account Actions (Mutating)</h2>
    <p>These actions require server mutation and are outside Phase B2/C execution scope.</p>
    <button id="btn-save" class="primary-btn" type="submit">Save Profile Changes</button>
    <button id="btn-upload" class="primary-btn">Upload Avatar Image</button>
    <button id="btn-delete" class="danger-btn">Delete Account Permanently</button>
  </div>

  <script>
    // Tab switching logic
    const tabs = document.querySelectorAll('[role="tab"]');
    const panels = document.querySelectorAll('[role="tabpanel"]');

    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.forEach(t => t.setAttribute('aria-selected', 'false'));
        panels.forEach(p => p.hidden = true);

        tab.setAttribute('aria-selected', 'true');
        const targetId = tab.getAttribute('aria-controls');
        const targetPanel = document.getElementById(targetId);
        if (targetPanel) {
          targetPanel.hidden = false;
        }
      });
    });

    // Working disclosures
    ['acc-employment', 'acc-2fa'].forEach(id => {
      const btn = document.getElementById(id);
      if (btn) {
        btn.addEventListener('click', () => {
          const isExp = btn.getAttribute('aria-expanded') === 'true';
          btn.setAttribute('aria-expanded', isExp ? 'false' : 'true');
          const target = document.getElementById(btn.getAttribute('aria-controls'));
          if (target) {
            target.hidden = isExp;
          }
        });
      }
    });

    // Delayed disclosure (150ms delay)
    const delayedBtn = document.getElementById('acc-delayed');
    if (delayedBtn) {
      delayedBtn.addEventListener('click', () => {
        const isExp = delayedBtn.getAttribute('aria-expanded') === 'true';
        delayedBtn.setAttribute('aria-expanded', isExp ? 'false' : 'true');
        setTimeout(() => {
          const target = document.getElementById(delayedBtn.getAttribute('aria-controls'));
          if (target) {
            target.hidden = isExp;
          }
        }, 150);
      });
    }

    // Safe Modal Dialog logic
    const historyDialogBtn = document.getElementById('btn-view-history-dialog');
    const historyDialog = document.getElementById('dialog-employment-history');
    const closeHistoryBtn = document.getElementById('btn-close-history-dialog');
    if (historyDialogBtn && historyDialog) {
      historyDialogBtn.addEventListener('click', () => {
        if (typeof historyDialog.showModal === 'function') {
          historyDialog.showModal();
        } else {
          historyDialog.setAttribute('open', '');
        }
      });
    }
    if (closeHistoryBtn && historyDialog) {
      closeHistoryBtn.addEventListener('click', () => {
        if (typeof historyDialog.close === 'function') {
          historyDialog.close();
        } else {
          historyDialog.removeAttribute('open');
        }
      });
    }

    // Quick Actions menu logic
    const actionsBtn = document.getElementById('btn-actions-menu');
    const actionsMenu = document.getElementById('menu-actions');
    if (actionsBtn && actionsMenu) {
      actionsBtn.addEventListener('click', () => {
        const isExp = actionsBtn.getAttribute('aria-expanded') === 'true';
        actionsBtn.setAttribute('aria-expanded', isExp ? 'false' : 'true');
        actionsMenu.hidden = isExp;
      });
    }
    const auditItem = document.getElementById('menu-item-audit');
    if (auditItem) {
      auditItem.addEventListener('click', () => {
        const p = document.getElementById('panel-audit');
        if (p) p.hidden = !p.hidden;
      });
    }
    const brokenItem = document.getElementById('menu-item-broken');
    if (brokenItem) {
      brokenItem.addEventListener('click', () => {
        brokenItem.setAttribute('aria-expanded', 'true');
      });
    }

    // Safe Local Form Date Validation logic
    const validateHistoryBtn = document.getElementById('btn-validate-history');
    const startDateInput = document.getElementById('input-start-date');
    const endDateInput = document.getElementById('input-end-date');
    const historyFeedback = document.getElementById('history-date-feedback');
    if (validateHistoryBtn && startDateInput && endDateInput && historyFeedback) {
      validateHistoryBtn.addEventListener('click', () => {
        window.__qa_click_count = (window.__qa_click_count || 0) + 1;
        const sVal = startDateInput.value || startDateInput.dataset.value || startDateInput.textContent.trim();
        const eVal = endDateInput.value || endDateInput.dataset.value || endDateInput.textContent.trim();
        if (sVal && eVal && eVal < sVal) {
          historyFeedback.textContent = 'End date cannot be earlier than start date.';
          historyFeedback.hidden = false;
          historyFeedback.setAttribute('data-validation-case', 'case_invalid_interval');
        } else {
          historyFeedback.textContent = 'Dates are valid.';
          historyFeedback.hidden = false;
          historyFeedback.setAttribute('data-validation-case', 'case_valid_interval');
        }
        historyFeedback.setAttribute('data-validation-timestamp', Date.now().toString());
      });
    }

    // Inert button has no click handler (remains inert)

    // Dangling button toggles its aria-expanded attribute
    const danglingBtn = document.getElementById('btn-dangling');
    if (danglingBtn) {
      danglingBtn.addEventListener('click', () => {
        const isExp = danglingBtn.getAttribute('aria-expanded') === 'true';
        danglingBtn.setAttribute('aria-expanded', isExp ? 'false' : 'true');
      });
    }

    // Unrevealed button toggles aria-expanded but fails to unhide panel
    const unrevealedBtn = document.getElementById('btn-unrevealed');
    if (unrevealedBtn) {
      unrevealedBtn.addEventListener('click', () => {
        unrevealedBtn.setAttribute('aria-expanded', 'true');
        // Deliberately keep target panel hidden
      });
    }

    // Crashing button throws uncaught TypeError
    const crashBtn = document.getElementById('btn-crash');
    if (crashBtn) {
      crashBtn.addEventListener('click', () => {
        null.failingPropertyAccess();
      });
    }

    // Experience test entries management
    const btnAddExp = document.getElementById('btn-add-experience');
    const dlgAddExp = document.getElementById('dialog-add-experience');
    if (btnAddExp && dlgAddExp) {
      btnAddExp.addEventListener('click', () => {
        document.getElementById('exp-company').value = '';
        document.getElementById('exp-role').value = '';
        document.getElementById('exp-desc').value = '';
        const fb = document.getElementById('exp-feedback');
        if (fb) fb.style.display = 'none';
        if (typeof dlgAddExp.showModal === 'function') {
          dlgAddExp.showModal();
        } else {
          dlgAddExp.setAttribute('open', '');
        }
      });
    }
    window.submitAddExperience = async function() {
      const comp = (document.getElementById('exp-company').value || '').trim();
      const role = (document.getElementById('exp-role').value || '').trim();
      const desc = (document.getElementById('exp-desc').value || '').trim();
      const fb = document.getElementById('exp-feedback');
      if (!comp || !role) {
        if (fb) {
          fb.textContent = 'Company and Role are required.';
          fb.style.display = 'block';
        }
        return;
      }
      if (fb) fb.style.display = 'none';

      let newId = 'exp-item-' + Math.random().toString(36).substr(2, 6);
      try {
        const resp = await fetch('/api/profile/experience', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ company: comp, role: role, desc: desc })
        });
        if (resp.ok) {
          const data = await resp.json();
          if (data && data.id) newId = data.id;
        }
      } catch (e) {
        console.warn('POST failed:', e);
      }

      const list = document.getElementById('experience-list');
      const itemEl = document.createElement('div');
      itemEl.className = 'exp-item';
      itemEl.id = newId;
      itemEl.setAttribute('data-id', newId);
      itemEl.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:10px;background:white;border:1px solid #e2e8f0;border-radius:6px;';
      itemEl.innerHTML = '<div><strong class="exp-title">' + comp + '</strong><p style="margin:2px 0 0;font-size:13px;color:#64748b;">' + role + ' - ' + desc + '</p></div><div style="display:flex;gap:6px;"><button type="button" id="btn-edit-experience" class="primary-btn" style="padding:4px 8px;font-size:12px;">Edit</button><button type="button" id="btn-delete-' + newId + '" class="danger-btn" style="padding:4px 8px;font-size:12px;">Delete</button></div>';
      const editBtn = itemEl.querySelector('#btn-edit-experience');
      if (editBtn) editBtn.addEventListener('click', function() { openEdit(newId, comp); });
      const delBtn = itemEl.querySelector('#btn-delete-' + newId);
      if (delBtn) delBtn.addEventListener('click', function() { openDelete(newId, comp); });
      list.appendChild(itemEl);
      if (typeof dlgAddExp.close === 'function') {
        dlgAddExp.close();
      } else {
        dlgAddExp.removeAttribute('open');
      }
    };
    window.openEdit = function(id, name) {
      document.getElementById('edit-exp-id').value = id;
      document.getElementById('edit-exp-company').value = name;
      const dlg = document.getElementById('dialog-edit-experience');
      if (dlg) {
        if (typeof dlg.showModal === 'function') dlg.showModal();
        else dlg.setAttribute('open', '');
      }
    };
    window.submitEditExperience = async function() {
      const id = document.getElementById('edit-exp-id').value;
      const newComp = (document.getElementById('edit-exp-company').value || '').trim();
      try {
        await fetch('/api/profile/experience/' + encodeURIComponent(id), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ company: newComp })
        });
      } catch (e) {
        console.warn('PUT failed:', e);
      }
      const item = document.getElementById(id);
      if (item && newComp) {
        const titleEl = item.querySelector('.exp-title');
        if (titleEl) titleEl.textContent = newComp;
      }
      const dlg = document.getElementById('dialog-edit-experience');
      if (dlg) {
        if (typeof dlg.close === 'function') dlg.close();
        else dlg.removeAttribute('open');
      }
    };
    window.openDelete = function(id, name) {
      document.getElementById('delete-target-id').value = id;
      document.getElementById('delete-confirm-text').textContent = "Are you sure you want to delete this test entry?";
      const dlg = document.getElementById('dialog-confirm-delete');
      if (dlg) {
        if (typeof dlg.showModal === 'function') dlg.showModal();
        else dlg.setAttribute('open', '');
      }
    };
    window.confirmDeleteExperience = async function() {
      const id = document.getElementById('delete-target-id').value;
      let deleteOk = true;
      try {
        const resp = await fetch('/api/profile/experience/' + encodeURIComponent(id), {
          method: 'DELETE'
        });
        if (!resp.ok) {
          deleteOk = false;
        }
      } catch (e) {
        console.warn('DELETE error:', e);
      }

      if (deleteOk || window.__qa_fail_delete_optimistically) {
        const item = document.getElementById(id);
        if (item) {
          item.remove();
        }
      }
      const dlg = document.getElementById('dialog-confirm-delete');
      if (dlg) {
        if (typeof dlg.close === 'function') dlg.close();
        else dlg.removeAttribute('open');
      }
    };
    window.deleteAttachment = async function(attId) {
      try {
        await fetch('/api/profile/attachments/' + encodeURIComponent(attId), {
          method: 'DELETE'
        });
      } catch (e) {}
      const item = document.getElementById(attId);
      if (item) item.remove();
      const fb = document.getElementById('upload-feedback');
      if (fb) fb.textContent = 'Attachment removed.';
    };
    window.submitUploadFile = async function() {
      const fi = document.getElementById('input-experience-file');
      const fb = document.getElementById('upload-feedback');
      if (fi && fi.files && fi.files.length > 0 && fb) {
        const file = fi.files[0];
        const fn = file.name;
        let attId = 'att-' + Math.random().toString(36).substr(2, 6);
        try {
          const fd = new FormData();
          fd.append('document', file, fn);
          const resp = await fetch('/api/profile/attachments/upload', {
            method: 'POST',
            body: fd
          });
          if (resp.ok) {
            const data = await resp.json();
            if (data && data.id) attId = data.id;
          }
        } catch (e) {
          console.warn('Upload error:', e);
        }

        fb.textContent = 'Upload complete: ' + fn + ' (' + file.size + ' bytes) attached.';
        fb.style.display = 'block';
        let list = document.getElementById('attachment-list');
        if (!list) {
          list = document.createElement('div');
          list.id = 'attachment-list';
          list.style.cssText = 'margin-top:8px;display:flex;flex-direction:column;gap:6px;';
          fb.parentNode.appendChild(list);
        }
        const item = document.createElement('div');
        item.className = 'attachment-item';
        item.id = attId;
        item.setAttribute('data-attachment-id', attId);
        item.setAttribute('data-id', attId);
        item.style.cssText = 'display:flex;justify-content:space-between;align-items:center;padding:6px 10px;background:#f8fafc;border:1px solid #cbd5e1;border-radius:4px;font-size:13px;';
        item.innerHTML = '<span>' + fn + '</span><button type="button" class="danger-btn" data-delete-attachment="' + attId + '" data-filename="' + fn + '" style="padding:2px 8px;font-size:12px;">Delete Attachment</button>';
        const delBtn = item.querySelector('button');
        if (delBtn) {
          delBtn.addEventListener('click', function() {
            deleteAttachment(attId);
          });
        }
        list.appendChild(item);
      }
    };
  </script>
</body>
</html>
"""


PROFILE_VARIED_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>User Profile (Varied DOM) - example.com simulation</title>
  <style>
    body { font-family: sans-serif; margin: 20px; color: #111; }
    .var-tablist { display: flex; gap: 8px; border-bottom: 2px solid #ccc; padding-bottom: 4px; }
    .var-tab { padding: 6px 14px; border: 1px solid #ccc; background: #eee; cursor: pointer; border-radius: 4px 4px 0 0; }
    .var-tab[aria-selected="true"] { background: #004d40; color: white; font-weight: bold; border-color: #004d40; }
    .var-panel { padding: 16px; border: 1px solid #ddd; border-top: none; margin-bottom: 16px; }
    .var-acc { padding: 8px 12px; margin: 6px 0; display: block; cursor: pointer; border: 1px solid #bbb; background: #f0f4f8; }
    .var-acc-panel { padding: 8px 12px; background: #e8f5e9; border-left: 4px solid #004d40; margin-bottom: 8px; }
    .danger-btn { background: #c62828; color: white; border: none; padding: 6px 12px; cursor: pointer; }
    .primary-btn { background: #004d40; color: white; border: none; padding: 6px 12px; cursor: pointer; }
    [hidden] { display: none !important; }
  </style>
</head>
<body>
  <h1>Account Center (Live Varied DOM Hierarchy)</h1>
  <p>Structure: Inverted tabs, varied labels, and anonymous (auto-ID) controls.</p>

  <!-- Inverted Tablist with Auto-ID elements (no explicit id on tabs) -->
  <div role="tablist" class="var-tablist" aria-label="Account Tabs">
    <button role="tab" class="var-tab" aria-selected="false" aria-controls="panel-sec-v">Security &amp; Credentials</button>
    <button role="tab" class="var-tab" aria-selected="true" aria-controls="panel-gen-v">Employee Overview</button>
  </div>

  <!-- Panels in varied order -->
  <section id="panel-gen-v" role="tabpanel" class="var-panel">
    <h2>Overview Data</h2>
    <p>User: Tester | Status: Active Staff</p>
    <p><label>Display Title: <input type="text" name="display_title" value="QA Lead"></label></p>
    <p><label>Profile Notes: <input type="text" name="v_notes" value="Lead Specialist"></label></p>
    
    <!-- Working accordion WITHOUT explicit id attribute -->
    <button class="var-acc" role="button" aria-expanded="false" aria-controls="panel-hist-v">Work History &amp; Employment</button>
    <div id="panel-hist-v" class="var-acc-panel" hidden>
      <p>Position: Senior QA Engineer | Tenure: 5 years</p>
    </div>

    <!-- Working modal dialog WITHOUT explicit id attribute on trigger button -->
    <div style="margin: 8px 0;">
      <button class="var-acc" role="button" aria-haspopup="dialog" aria-controls="dialog-varied-history">View Career Record</button>
      <dialog id="dialog-varied-history" style="padding: 16px; border: 2px solid #004d40; border-radius: 6px; background: white;">
        <h4>Career Record Details</h4>
        <p>Tenure: 5 years | Active Staff</p>
        <button type="button" class="danger-btn" id="btn-close-v-dialog" style="cursor:pointer; margin-top:10px;">Close Record</button>
      </dialog>
    </div>

    <!-- Working local date validation WITHOUT explicit id attribute on trigger button -->
    <div style="margin: 8px 0; padding: 10px; background: #e8f5e9; border: 1px solid #c8e6c9;">
      <div style="margin-bottom: 6px; display: flex; gap: 8px;">
        <label>Start Date: <input type="date" id="v-start-date" value="2024-05-01"></label>
        <label>End Date: <input type="date" id="v-end-date" value="2022-01-01"></label>
      </div>
      <button class="var-acc" role="button" type="button" aria-controls="v-date-feedback">Validate Period Dates</button>
      <div id="v-date-feedback" role="alert" style="margin-top: 6px; color: #c62828; font-weight: bold;" hidden>
        End date cannot be earlier than start date.
      </div>
    </div>

    <!-- Working dropdown menu WITHOUT explicit id attribute on trigger button -->
    <div style="margin: 8px 0; position: relative;">
      <button class="var-acc" role="button" aria-haspopup="menu" aria-expanded="false" aria-controls="var-menu-actions">Quick Options</button>
      <div id="var-menu-actions" role="menu" class="var-acc-panel" hidden>
        <button role="menuitem" class="var-acc" aria-controls="var-panel-audit">Review History</button>
        <div id="var-panel-audit" hidden><p>Reviewed.</p></div>
      </div>
    </div>
  </section>

  <section id="panel-sec-v" role="tabpanel" class="var-panel" hidden>
    <h2>Access and Security</h2>
    <p>Protected area revealed behind tab.</p>
    <!-- Working 2FA accordion WITHOUT explicit id attribute -->
    <button class="var-acc" role="button" aria-expanded="false" aria-controls="panel-mfa-v">Two-Factor Authentication Setup</button>
    <div id="panel-mfa-v" class="var-acc-panel" hidden>
      <p>Authenticator app configured and operational.</p>
    </div>
    <div style="margin-top: 12px;">
      <button class="danger-btn" id="btn-purge-keys">Revoke All Security Keys</button>
    </div>
  </section>

  <!-- Deliberately Broken & Inert Controls -->
  <section style="margin-top: 16px; padding: 12px; background: #fffde7; border: 1px solid #fff59d;">
    <h3>Integrity Diagnostics</h3>

    <!-- Dangling target without id on button -->
    <button class="var-acc" role="button" aria-expanded="false" aria-controls="ghost-nonexistent-box">Dangling Target Button</button>

    <!-- Unrevealed target without id on button -->
    <button class="var-acc" role="button" aria-expanded="false" aria-controls="panel-unrevealed-v">Unrevealed Content Button</button>
    <div id="panel-unrevealed-v" class="var-acc-panel" hidden>
      <p>Hidden content that never appears.</p>
    </div>

    <!-- Crashing button -->
    <button class="var-acc" role="button" id="v-crash-btn" aria-expanded="false" aria-controls="panel-crash-v">Crash On Click Button</button>
    <div id="panel-crash-v" class="var-acc-panel" hidden>
      <p>Crashing button content.</p>
    </div>

    <!-- Inert button -->
    <button class="var-acc" role="button" id="v-inert-btn" aria-expanded="false" aria-controls="panel-inert-v">Inert Disclosure Button</button>
    <div id="panel-inert-v" class="var-acc-panel" hidden>
      <p>Inert content.</p>
    </div>

    <!-- Delayed transition accordion (150ms) -->
    <button class="var-acc" role="button" id="v-delayed-btn" aria-expanded="false" aria-controls="panel-delayed-v">Delayed Transition Details</button>
    <div id="panel-delayed-v" class="var-acc-panel" hidden>
      <p>Asynchronously revealed content settled after 150ms.</p>
    </div>
  </section>

  <!-- Mutating / Prohibited actions -->
  <section style="margin-top: 16px; padding: 12px; background: #ffebee; border: 1px solid #ffcdd2;">
    <h3>Server Actions</h3>
    <button type="submit" class="primary-btn">Save Profile Changes</button>
    <button type="button" class="primary-btn">Upload Avatar Image</button>
    <button type="button" class="danger-btn">Delete Account Permanently</button>
  </section>

  <script>
    // Tab switching
    const tabs = document.querySelectorAll('[role="tab"]');
    const panels = document.querySelectorAll('[role="tabpanel"]');

    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.forEach(t => t.setAttribute('aria-selected', 'false'));
        panels.forEach(p => p.hidden = true);
        tab.setAttribute('aria-selected', 'true');
        const targetId = tab.getAttribute('aria-controls');
        const panel = document.getElementById(targetId);
        if (panel) panel.hidden = false;
      });
    });

    // Standard working disclosures
    document.querySelectorAll('.var-acc').forEach(btn => {
      const controlsId = btn.getAttribute('aria-controls');
      if (controlsId === 'panel-hist-v' || controlsId === 'panel-mfa-v') {
        btn.addEventListener('click', () => {
          const isExp = btn.getAttribute('aria-expanded') === 'true';
          btn.setAttribute('aria-expanded', isExp ? 'false' : 'true');
          const p = document.getElementById(controlsId);
          if (p) p.hidden = isExp;
        });
      } else if (controlsId === 'dialog-varied-history') {
        btn.addEventListener('click', () => {
          const d = document.getElementById(controlsId);
          if (d) {
            if (typeof d.showModal === 'function') d.showModal();
            else d.setAttribute('open', '');
          }
        });
      } else if (controlsId === 'v-date-feedback') {
        btn.addEventListener('click', () => {
          const s = document.getElementById('v-start-date');
          const e = document.getElementById('v-end-date');
          const fb = document.getElementById('v-date-feedback');
          const sVal = s ? (s.value || s.dataset.value || s.textContent.trim()) : '';
          const eVal = e ? (e.value || e.dataset.value || e.textContent.trim()) : '';
          if (sVal && eVal && eVal < sVal && fb) {
            fb.textContent = 'End date cannot be earlier than start date.';
            fb.hidden = false;
            fb.setAttribute('data-validation-case', 'v_case_invalid');
            fb.setAttribute('data-validation-timestamp', Date.now().toString());
          } else if (sVal && eVal && fb) {
            fb.textContent = 'Dates are valid.';
            fb.hidden = false;
            fb.setAttribute('data-validation-case', 'v_case_valid');
            fb.setAttribute('data-validation-timestamp', Date.now().toString());
          }
        });
      } else if (controlsId === 'var-menu-actions') {
        btn.addEventListener('click', () => {
          const isExp = btn.getAttribute('aria-expanded') === 'true';
          btn.setAttribute('aria-expanded', isExp ? 'false' : 'true');
          const m = document.getElementById(controlsId);
          if (m) m.hidden = isExp;
        });
      }
    });

    document.querySelectorAll('[role="menuitem"]').forEach(item => {
      item.addEventListener('click', () => {
        const targetId = item.getAttribute('aria-controls');
        if (targetId) {
          const target = document.getElementById(targetId);
          if (target) target.hidden = !target.hidden;
        }
      });
    });

    const closeVDialog = document.getElementById('btn-close-v-dialog');
    if (closeVDialog) {
      closeVDialog.addEventListener('click', () => {
        const d = document.getElementById('dialog-varied-history');
        if (d) {
          if (typeof d.close === 'function') d.close();
          else d.removeAttribute('open');
        }
      });
    }

    // Delayed disclosure (150ms)
    const delBtn = document.getElementById('v-delayed-btn');
    if (delBtn) {
      delBtn.addEventListener('click', () => {
        const isExp = delBtn.getAttribute('aria-expanded') === 'true';
        delBtn.setAttribute('aria-expanded', isExp ? 'false' : 'true');
        setTimeout(() => {
          const p = document.getElementById('panel-delayed-v');
          if (p) p.hidden = isExp;
        }, 150);
      });
    }

    // Dangling button toggles aria-expanded
    const danglingBtn = document.querySelector('[aria-controls="ghost-nonexistent-box"]');
    if (danglingBtn) {
      danglingBtn.addEventListener('click', () => {
        const isExp = danglingBtn.getAttribute('aria-expanded') === 'true';
        danglingBtn.setAttribute('aria-expanded', isExp ? 'false' : 'true');
      });
    }

    // Unrevealed button toggles aria-expanded but leaves panel hidden
    const unrevealedBtn = document.querySelector('[aria-controls="panel-unrevealed-v"]');
    if (unrevealedBtn) {
      unrevealedBtn.addEventListener('click', () => {
        unrevealedBtn.setAttribute('aria-expanded', 'true');
      });
    }

    // Crash button throws
    const crashBtn = document.getElementById('v-crash-btn');
    if (crashBtn) {
      crashBtn.addEventListener('click', () => {
        null.failingPropertyAccess();
      });
    }
  </script>
</body>
</html>
"""


def get_validation_variant_html(variant: str) -> bytes:
    """Generate profile fixture HTML with specific validation behavior for browser testing."""
    html_str = PROFILE_HTML.decode("utf-8")
    old_logic = """        if (sVal && eVal && eVal < sVal) {
          historyFeedback.textContent = 'End date cannot be earlier than start date.';
          historyFeedback.hidden = false;
          historyFeedback.setAttribute('data-validation-case', 'case_invalid_interval');
        } else {
          historyFeedback.textContent = 'Dates are valid.';
          historyFeedback.hidden = false;
          historyFeedback.setAttribute('data-validation-case', 'case_valid_interval');
        }
        historyFeedback.setAttribute('data-validation-timestamp', Date.now().toString());"""

    if variant == "accept_invalid":
        # Broken validator: always displays 'Dates are valid.', accepting invalid input
        new_logic = """        historyFeedback.textContent = 'Dates are valid.';
        historyFeedback.hidden = false;
        historyFeedback.setAttribute('data-validation-case', sVal && eVal && eVal < sVal ? 'case_invalid_interval' : 'case_valid_interval');
        historyFeedback.setAttribute('data-validation-timestamp', Date.now().toString());"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "reject_valid":
        # Broken validator: always displays 'End date cannot be earlier than start date.', rejecting valid input
        new_logic = """        historyFeedback.textContent = 'End date cannot be earlier than start date.';
        historyFeedback.hidden = false;
        historyFeedback.setAttribute('data-validation-case', sVal && eVal && eVal < sVal ? 'case_invalid_interval' : 'case_valid_interval');
        historyFeedback.setAttribute('data-validation-timestamp', Date.now().toString());"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "delayed_feedback":
        # Delayed feedback: updates feedback text after 300ms
        new_logic = """        setTimeout(() => {
          if (sVal && eVal && eVal < sVal) {
            historyFeedback.textContent = 'End date cannot be earlier than start date.';
            historyFeedback.setAttribute('data-validation-case', 'case_invalid_interval');
          } else {
            historyFeedback.textContent = 'Dates are valid.';
            historyFeedback.setAttribute('data-validation-case', 'case_valid_interval');
          }
          historyFeedback.hidden = false;
          historyFeedback.setAttribute('data-validation-timestamp', Date.now().toString());
        }, 300);"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "noop_trigger":
        # Pre-existing expected text with a no-op validation trigger:
        # Feedback element starts pre-populated and visible with expected error text.
        # Button click does nothing to the feedback element.
        html_str = html_str.replace(
            '<div id="history-date-feedback" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;" hidden></div>',
            '<div id="history-date-feedback" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;">\n        End date cannot be earlier than start date.\n      </div>'
        )
        new_logic = """        /* no-op trigger: no mutation or text change on feedback element */"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "cosmetic_only":
        # Adversarial reproduction from review: pre-existing feedback text with a cosmetic class toggle only.
        html_str = html_str.replace(
            '<div id="history-date-feedback" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;" hidden></div>',
            '<div id="history-date-feedback" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;">\n        End date cannot be earlier than start date.\n      </div>'
        )
        new_logic = """        /* cosmetic-only: toggles cosmetic CSS class without validating */
        historyFeedback.classList.toggle('cosmetic-highlight');"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "stale_marker":
        # Adversarial stale-marker reproduction from review:
        # Pre-existing data-validation-case="case_valid_interval" with stale error text and cosmetic class toggle only.
        html_str = html_str.replace(
            '<div id="history-date-feedback" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;" hidden></div>',
            '<div id="history-date-feedback" data-validation-case="case_valid_interval" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;">\n        End date cannot be earlier than start date.\n      </div>'
        )
        new_logic = """        /* cosmetic-only: toggles cosmetic CSS class without validating */
        historyFeedback.classList.toggle('cosmetic-highlight');"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "stale_marker_loading_only":
        # Adversarial loading-only bypass reproduction from review:
        # Pre-existing data-validation-case="case_valid_interval" with stale error text and loading CSS class cycle only.
        html_str = html_str.replace(
            '<div id="history-date-feedback" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;" hidden></div>',
            '<div id="history-date-feedback" data-validation-case="case_valid_interval" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;">\n        End date cannot be earlier than start date.\n      </div>'
        )
        new_logic = """        /* loading-only: adds and removes loading class without validating */
        historyFeedback.classList.add('loading');
        setTimeout(() => historyFeedback.classList.remove('loading'), 120);"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "stale_marker_malformed_timestamp":
        # Pre-existing matching marker with malformed non-numeric timestamp attribute.
        html_str = html_str.replace(
            '<div id="history-date-feedback" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;" hidden></div>',
            '<div id="history-date-feedback" data-validation-case="case_valid_interval" data-validation-timestamp="not_a_number" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;">\n        End date cannot be earlier than start date.\n      </div>'
        )
        new_logic = """        /* sets malformed timestamp on click */
        historyFeedback.setAttribute('data-validation-timestamp', 'also_not_a_number');"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "stale_marker_non_increasing_timestamp":
        # Pre-existing matching marker with non-increasing timestamp attribute.
        html_str = html_str.replace(
            '<div id="history-date-feedback" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;" hidden></div>',
            '<div id="history-date-feedback" data-validation-case="case_valid_interval" data-validation-timestamp="2000" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;">\n        End date cannot be earlier than start date.\n      </div>'
        )
        new_logic = """        /* sets identical timestamp (non-increasing) */
        historyFeedback.setAttribute('data-validation-timestamp', '2000');"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "stale_marker_working_update":
        # Prior matching marker remains from earlier run (prepopulated data-validation-case="case_valid_interval" and timestamp=1000).
        # Trigger click genuinely executes validation and updates data-validation-timestamp with a fresh timestamp.
        html_str = html_str.replace(
            '<div id="history-date-feedback" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;" hidden></div>',
            '<div id="history-date-feedback" data-validation-case="case_valid_interval" data-validation-timestamp="1000" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;">\n        Dates are valid.\n      </div>'
        )
        new_logic = """        if (sVal && eVal && eVal < sVal) {
          historyFeedback.textContent = 'End date cannot be earlier than start date.';
          historyFeedback.hidden = false;
          historyFeedback.setAttribute('data-validation-case', 'case_invalid_interval');
        } else {
          historyFeedback.textContent = 'Dates are valid.';
          historyFeedback.hidden = false;
          historyFeedback.setAttribute('data-validation-case', 'case_valid_interval');
        }
        historyFeedback.setAttribute('data-validation-timestamp', Date.now().toString());"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "repeated_run_no_timestamp":
        # First execution sets marker and timestamp; repeated execution does NOT update timestamp or marker.
        new_logic = """        if (!window._validation_run_count) {
          window._validation_run_count = 1;
          historyFeedback.textContent = 'Dates are valid.';
          historyFeedback.hidden = false;
          historyFeedback.setAttribute('data-validation-case', 'case_valid_interval');
          historyFeedback.setAttribute('data-validation-timestamp', Date.now().toString());
        } else {
          window._validation_run_count++;
          historyFeedback.classList.toggle('cosmetic-highlight');
        }"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "loading_presentation_transition":
        # Loading/presentation update before the real result arrives:
        # Immediately sets aria-busy and validating class/text, then delivers real result after 120ms.
        new_logic = """        historyFeedback.setAttribute('aria-busy', 'true');
        historyFeedback.classList.add('validating');
        historyFeedback.textContent = 'Validating dates...';
        historyFeedback.hidden = false;
        setTimeout(() => {
          historyFeedback.setAttribute('aria-busy', 'false');
          historyFeedback.classList.remove('validating');
          if (sVal && eVal && eVal < sVal) {
            historyFeedback.textContent = 'End date cannot be earlier than start date.';
            historyFeedback.setAttribute('data-validation-case', 'case_invalid_interval');
          } else {
            historyFeedback.textContent = 'Dates are valid.';
            historyFeedback.setAttribute('data-validation-case', 'case_valid_interval');
          }
          historyFeedback.setAttribute('data-validation-timestamp', Date.now().toString());
        }, 120);"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "cross_case_delayed_callback":
        # Delayed callback from an earlier case (Case 0) arrives while subsequent case (Case 1) is active:
        # Case 0 (valid: sVal === '2022-01-01') completes immediately, but also schedules a delayed callback
        # that fires after 200ms with Case 0's marker.
        # Case 1 (invalid: sVal === '2024-05-01') does NOT update feedback.
        # The delayed callback from Case 0 fires while Case 1 is polling.
        # Current-case attribution prevents Case 0's callback from contaminating Case 1.
        new_logic = """        if (sVal === '2022-01-01') {
          historyFeedback.textContent = 'Dates are valid.';
          historyFeedback.setAttribute('data-validation-case', 'case_valid_interval');
          historyFeedback.hidden = false;
          setTimeout(() => {
            historyFeedback.textContent = 'End date cannot be earlier than start date.';
            historyFeedback.setAttribute('data-validation-case', 'case_valid_interval');
            historyFeedback.hidden = false;
          }, 200);
        } else {
          /* Case 1 trigger is a no-op; only Case 0's delayed callback will fire during Case 1 */
        }"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "identical_text_new_completion":
        # Validator outputs identical text for consecutive cases, but genuinely completes each case with marker
        new_logic = """        historyFeedback.textContent = 'End date cannot be earlier than start date.';
        historyFeedback.hidden = false;
        const currentCase = (sVal === '2025-01-01') ? 'case_invalid_interval_2' : 'case_invalid_interval';
        historyFeedback.setAttribute('data-validation-case', currentCase);
        historyFeedback.setAttribute('data-validation-timestamp', Date.now().toString());"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "stale_unchanged_timeout":
        # Unchanged stale feedback after timeout:
        # Feedback element is visible with generic initial text; trigger does nothing.
        html_str = html_str.replace(
            '<div id="history-date-feedback" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;" hidden></div>',
            '<div id="history-date-feedback" role="alert" style="margin-top: 8px; color: #b91c1c; font-weight: bold;">\n        Awaiting input validation.\n      </div>'
        )
        new_logic = """        /* stale timeout trigger: no mutation on feedback */"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "late_preceding_feedback":
        # Late feedback from the preceding case:
        # Click handler triggers delayed update after 4000ms.
        new_logic = """        setTimeout(() => {
          if (sVal && eVal && eVal < sVal) {
            historyFeedback.textContent = 'End date cannot be earlier than start date.';
            historyFeedback.setAttribute('data-validation-case', 'case_invalid_interval');
          } else {
            historyFeedback.textContent = 'Dates are valid.';
            historyFeedback.setAttribute('data-validation-case', 'case_valid_interval');
          }
          historyFeedback.hidden = false;
          historyFeedback.setAttribute('data-validation-timestamp', Date.now().toString());
        }, 4000);"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "working_validator_fresh":
        # Working validator with fresh results:
        new_logic = """        if (sVal && eVal && eVal < sVal) {
          historyFeedback.textContent = 'End date cannot be earlier than start date.';
          historyFeedback.hidden = false;
          historyFeedback.setAttribute('data-validation-case', 'case_invalid_interval');
        } else {
          historyFeedback.textContent = 'Dates are valid.';
          historyFeedback.hidden = false;
          historyFeedback.setAttribute('data-validation-case', 'case_valid_interval');
        }
        historyFeedback.setAttribute('data-validation-timestamp', Date.now().toString());"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "observation_timeout_slow":
        # Slow validator that exceeds short deadlines (1200ms delay)
        new_logic = """        setTimeout(() => {
          if (sVal && eVal && eVal < sVal) {
            historyFeedback.textContent = 'End date cannot be earlier than start date.';
            historyFeedback.setAttribute('data-validation-case', 'case_invalid_interval');
          } else {
            historyFeedback.textContent = 'Dates are valid.';
            historyFeedback.setAttribute('data-validation-case', 'case_valid_interval');
          }
          historyFeedback.hidden = false;
          historyFeedback.setAttribute('data-validation-timestamp', Date.now().toString());
        }, 1200);"""
        html_str = html_str.replace(old_logic, new_logic)
    elif variant == "missing_field":
        # Missing input field: remove input-end-date element
        html_str = html_str.replace(
            '<input type="date" id="input-end-date" value="2022-01-01">',
            '<!-- missing input-end-date field -->'
        )
    elif variant == "ambiguous_field":
        # Ambiguous input field: duplicate input-start-date element
        html_str = html_str.replace(
            '<input type="date" id="input-start-date" value="2024-05-01">',
            '<input type="date" id="input-start-date" value="2024-05-01"><input type="date" id="input-start-date" value="2024-05-01">'
        )
    elif variant == "blocked_network":
        # Mutating network activity attempted during validation (blocked by QA lockdown barrier)
        network_logic = """        try { fetch('/api/network-submit-attempt', { method: 'POST', body: 'leak' }); } catch(e) {}
        if (sVal && eVal && eVal < sVal) {"""
        html_str = html_str.replace("""        if (sVal && eVal && eVal < sVal) {""", network_logic)

    return html_str.encode("utf-8")


EXPERIENCE_SECTION_HTML = b"""
    <!-- Authorized Test Data Section: Experience & Portfolio Entries -->
    <div id="section-experience-management" data-qa-inventory-complete="true" style="margin: 20px 0; padding: 16px; background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px;">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
        <h3 style="margin:0; font-size:16px;">Professional Experience &amp; Test Entries</h3>
        <button type="button" id="btn-add-experience" class="primary-btn" style="cursor:pointer;">+ Add Experience</button>
      </div>

      <!-- Add Experience Dialog / Form -->
      <dialog id="dialog-add-experience" style="padding: 20px; border: 2px solid #2563eb; border-radius: 8px; background: white; max-width: 480px; width: 100%;">
        <h4 style="margin-top:0;">Add Experience Entry</h4>
        <form id="form-add-experience" onsubmit="event.preventDefault();submitAddExperience();">
          <div style="margin-bottom:10px;">
            <label for="exp-company" style="display:block;font-weight:600;font-size:13px;margin-bottom:4px;">Company / Organization *</label>
            <input type="text" id="exp-company" name="company" placeholder="e.g. Nordic QA AB" style="width:100%;box-sizing:border-box;padding:6px 10px;">
          </div>
          <div style="margin-bottom:10px;">
            <label for="exp-role" style="display:block;font-weight:600;font-size:13px;margin-bottom:4px;">Role / Title *</label>
            <input type="text" id="exp-role" name="role" placeholder="e.g. Test Engineer" style="width:100%;box-sizing:border-box;padding:6px 10px;">
          </div>
          <div style="margin-bottom:12px;">
            <label for="exp-desc" style="display:block;font-weight:600;font-size:13px;margin-bottom:4px;">Description</label>
            <textarea id="exp-desc" name="description" rows="3" style="width:100%;box-sizing:border-box;padding:6px 10px;"></textarea>
          </div>
          <div id="exp-feedback" role="alert" style="color:#dc2626;font-size:13px;margin-bottom:10px;display:none;"></div>
          <div style="display:flex; gap:10px; justify-content:flex-end;">
            <button type="button" id="btn-cancel-add-experience" class="danger-btn" onclick="document.getElementById('dialog-add-experience').close();" style="background:#64748b;">Cancel</button>
            <button type="submit" id="btn-submit-experience" class="primary-btn">Save Experience</button>
          </div>
        </form>
      </dialog>

      <!-- Edit Experience Dialog -->
      <dialog id="dialog-edit-experience" style="padding: 20px; border: 2px solid #2563eb; border-radius: 8px; background: white; max-width: 480px; width: 100%;">
        <h4 style="margin-top:0;">Edit Experience Entry</h4>
        <form id="form-edit-experience" onsubmit="event.preventDefault();submitEditExperience();">
          <input type="hidden" id="edit-exp-id">
          <div style="margin-bottom:10px;">
            <label for="edit-exp-company" style="display:block;font-weight:600;font-size:13px;margin-bottom:4px;">Company *</label>
            <input type="text" id="edit-exp-company" style="width:100%;box-sizing:border-box;padding:6px 10px;">
          </div>
          <div style="display:flex; gap:10px; justify-content:flex-end;">
            <button type="button" class="danger-btn" onclick="document.getElementById('dialog-edit-experience').close();" style="background:#64748b;">Cancel</button>
            <button type="submit" id="btn-save-experience" class="primary-btn">Save Changes</button>
          </div>
        </form>
      </dialog>

      <!-- Delete Confirmation Dialog -->
      <dialog id="dialog-confirm-delete" style="padding: 20px; border: 2px solid #dc2626; border-radius: 8px; background: white; max-width: 400px; width: 100%;">
        <h4 style="margin-top:0;color:#dc2626;">Confirm Deletion</h4>
        <p id="delete-confirm-text">Are you sure you want to delete this test entry?</p>
        <input type="hidden" id="delete-target-id">
        <div style="display:flex; gap:10px; justify-content:flex-end;">
          <button type="button" id="btn-cancel-delete" class="danger-btn" onclick="document.getElementById('dialog-confirm-delete').close();" style="background:#64748b;">Cancel</button>
          <button type="button" id="btn-confirm-delete" class="danger-btn" onclick="confirmDeleteExperience();">Delete Permanently</button>
        </div>
      </dialog>

      <!-- List of Experience Items -->
      <div id="experience-list" style="display:flex; flex-direction:column; gap:8px;">
        <!-- Pre-existing user entry (must never be deleted by test run) -->
        <div class="exp-item" id="exp-preexisting-1" data-id="exp-preexisting-1" style="display:flex;justify-content:space-between;align-items:center;padding:10px;background:white;border:1px solid #e2e8f0;border-radius:6px;">
          <div>
            <strong class="exp-title">Nordic QA Governance (Pre-existing User Entry)</strong>
            <p style="margin:2px 0 0;font-size:13px;color:#64748b;">Principal QA Specialist - 2019-Present</p>
          </div>
          <div style="display:flex;gap:6px;">
            <button type="button" class="primary-btn" style="padding:4px 8px;font-size:12px;" onclick="openEdit('exp-preexisting-1', 'Nordic QA Governance (Pre-existing User Entry)')">Edit</button>
            <button type="button" class="danger-btn" id="btn-delete-exp-preexisting-1" style="padding:4px 8px;font-size:12px;" onclick="openDelete('exp-preexisting-1', 'Nordic QA Governance (Pre-existing User Entry)')">Delete</button>
          </div>
        </div>
      </div>

      <!-- File Attachment / Upload Area -->
      <div style="margin-top:16px;padding-top:14px;border-top:1px solid #e2e8f0;">
        <h4 style="margin:0 0 6px;font-size:14px;">Attach Certificate or Verification Document</h4>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
          <label for="input-experience-file" style="position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);border:0;">Upload verification document</label>
          <input type="file" id="input-experience-file" aria-label="Upload verification document" data-qa-upload-destination="/api/profile/attachments/upload">
          <button type="button" id="btn-upload-file" class="primary-btn" onclick="submitUploadFile();" style="padding:6px 14px;">Upload Document</button>
        </div>
        <div id="upload-feedback" role="status" style="margin-top:6px;font-size:13px;color:#059669;display:none;"></div>
      </div>
    </div>
"""

PROFILE_READONLY_HTML = PROFILE_HTML.replace(b"<!-- EXPERIENCE_MUTATION_SECTION_PLACEHOLDER -->", b"")
PROFILE_MUTATION_HTML = PROFILE_HTML.replace(b"<!-- EXPERIENCE_MUTATION_SECTION_PLACEHOLDER -->", EXPERIENCE_SECTION_HTML)
PROFILE_HTML = PROFILE_READONLY_HTML


class ProfileTestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    @property
    def test_server(self) -> "ProfileTestServer":
        return self.server  # type: ignore

    def _render_mutation_html(self) -> bytes:
        html_str = PROFILE_MUTATION_HTML.decode("utf-8")
        # Build dynamic experiences list HTML
        exp_items_html = ""
        for eid, exp in self.test_server.experiences.items():
            comp = exp.get("company", "")
            role = exp.get("role", "")
            desc = exp.get("desc", "")
            if eid == "exp-preexisting-1":
                exp_items_html += f"""
        <div class="exp-item" id="exp-preexisting-1" data-id="exp-preexisting-1" style="display:flex;justify-content:space-between;align-items:center;padding:10px;background:white;border:1px solid #e2e8f0;border-radius:6px;">
          <div>
            <strong class="exp-title">Nordic QA Governance (Pre-existing User Entry)</strong>
            <p style="margin:2px 0 0;font-size:13px;color:#64748b;">Principal QA Specialist - 2019-Present</p>
          </div>
          <div style="display:flex;gap:6px;">
            <button type="button" class="primary-btn" style="padding:4px 8px;font-size:12px;" onclick="openEdit('exp-preexisting-1', 'Nordic QA Governance (Pre-existing User Entry)')">Edit</button>
            <button type="button" class="danger-btn" id="btn-delete-exp-preexisting-1" style="padding:4px 8px;font-size:12px;" onclick="openDelete('exp-preexisting-1', 'Nordic QA Governance (Pre-existing User Entry)')">Delete</button>
          </div>
        </div>"""
            else:
                exp_items_html += f"""
        <div class="exp-item" id="{eid}" data-id="{eid}" style="display:flex;justify-content:space-between;align-items:center;padding:10px;background:white;border:1px solid #e2e8f0;border-radius:6px;">
          <div>
            <strong class="exp-title">{comp}</strong>
            <p style="margin:2px 0 0;font-size:13px;color:#64748b;">{role} - {desc}</p>
          </div>
          <div style="display:flex;gap:6px;">
            <button type="button" id="btn-edit-experience" class="primary-btn" style="padding:4px 8px;font-size:12px;" onclick="openEdit('{eid}', '{comp}')">Edit</button>
            <button type="button" id="btn-delete-{eid}" class="danger-btn" style="padding:4px 8px;font-size:12px;" onclick="openDelete('{eid}', '{comp}')">Delete</button>
          </div>
        </div>"""

        # Replace experience-list content
        start_exp = '<div id="experience-list" style="display:flex; flex-direction:column; gap:8px;">'
        if start_exp in html_str:
            prefix = html_str.split(start_exp)[0] + start_exp
            suffix = html_str.split(start_exp)[1]
            end_exp = '</div>\n\n      <!-- File Attachment'
            if end_exp in suffix:
                after_exp = suffix.split(end_exp)[1]
                html_str = prefix + exp_items_html + "\n      </div>\n\n      <!-- File Attachment" + after_exp

        # Build dynamic attachment list
        att_items_html = ""
        for aid, att in self.test_server.attachments.items():
            fn = att.get("filename", "")
            att_items_html += f"""
        <div class="attachment-item" id="{aid}" data-attachment-id="{aid}" data-id="{aid}" style="display:flex;justify-content:space-between;align-items:center;padding:6px 10px;background:#f8fafc;border:1px solid #cbd5e1;border-radius:4px;font-size:13px;">
          <span>{fn}</span>
          <button type="button" class="danger-btn" data-delete-attachment="{aid}" data-filename="{fn}" style="padding:2px 8px;font-size:12px;" onclick="deleteAttachment('{aid}')">Delete Attachment</button>
        </div>"""

        if att_items_html:
            placeholder = '<div id="upload-feedback" role="status" style="margin-top:6px;font-size:13px;color:#059669;display:none;"></div>'
            replacement = placeholder + f'\n        <div id="attachment-list" style="margin-top:8px;display:flex;flex-direction:column;gap:6px;">{att_items_html}\n        </div>'
            html_str = html_str.replace(placeholder, replacement)

        if not self.test_server.upload_destination_contract:
            html_str = html_str.replace(' data-qa-upload-destination="/api/profile/attachments/upload"', '')
        return html_str.encode("utf-8")

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if self.test_server.fail_readback_after_reloads is not None and len(self.test_server.experiences) > 1:
            self.test_server.reload_count += 1
            if self.test_server.reload_count >= self.test_server.fail_readback_after_reloads:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"Internal Server Error (Simulated Readback Failure)")
                return

        if url.path in ("/", "/profile", "/profile/mutation"):
            is_mutation = "mutation" in url.query or "mutation" in url.path or self.headers.get("X-QA-Mutation") == "1"
            body = self._render_mutation_html() if is_mutation else PROFILE_READONLY_HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        elif url.path in ("/profile-varied",):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PROFILE_VARIED_HTML)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(PROFILE_VARIED_HTML)
        elif url.path in ("/profile-validation-variant",):
            qs = urllib.parse.parse_qs(url.query)
            variant = qs.get("variant", ["correct"])[0]
            content = get_validation_variant_html(variant)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(content)
        elif url.path in ("/profile-unrelated",):
            content = b"<!DOCTYPE html><html><body><h1>Unrelated Page</h1><button type=\"button\" id=\"btn-validate-history\">Unrelated Button</button></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"Not Found")

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b""

        if "delete_account" in url.path or "account/delete" in url.path:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b'{"error": "Account deletion is strictly prohibited"}')
            return

        if "experience" in url.path:
            if getattr(self.test_server, "fail_create_server", False):
                status_code = getattr(self.test_server, "fail_create_status_code", 500)
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b'{"status": "error", "message": "Simulated create failure"}')
                return
            try:
                data = json.loads(post_data.decode("utf-8"))
            except Exception:
                qs = urllib.parse.parse_qs(post_data.decode("utf-8"))
                data = {k: v[0] for k, v in qs.items()}

            exp_id = f"exp-item-{self.test_server.next_exp_id}"
            self.test_server.next_exp_id += 1
            item = {
                "id": exp_id,
                "company": data.get("company", "Unknown Company"),
                "role": data.get("role", "Tester"),
                "desc": data.get("desc", ""),
            }
            self.test_server.experiences[exp_id] = item
            self.test_server.operations_log.append({
                "op": "CREATE",
                "entity": "experience",
                "id": exp_id,
                "data": dict(item),
            })
            resp_data = json.dumps({"status": "success", "id": exp_id, **item}).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(resp_data)
            return

        if "attachment" in url.path:
            if getattr(self.test_server, "fail_upload_server", False):
                status_code = getattr(self.test_server, "fail_upload_status_code", 500)
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b'{"status": "error", "message": "Simulated upload failure"}')
                return
            att_id = f"att-{self.test_server.next_att_id}"
            self.test_server.next_att_id += 1
            fn = f"uploaded_{att_id}.txt"
            if b'filename="' in post_data:
                parts = post_data.split(b'filename="')
                if len(parts) > 1:
                    fn = parts[1].split(b'"')[0].decode("utf-8", "ignore")
            self.test_server.attachments[att_id] = {"id": att_id, "filename": fn, "content": post_data}
            self.test_server.operations_log.append({
                "op": "UPLOAD",
                "entity": "attachment",
                "id": att_id,
                "filename": fn,
                "path": url.path,
                "content_type": self.headers.get("Content-Type", ""),
                "body_b64": __import__("base64").b64encode(post_data).decode("ascii"),
            })
            resp_data = json.dumps({"status": "success", "id": att_id, "filename": fn}).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(resp_data)
            return

        if url.path.startswith("/api/profile/"):
            resp_data = b'{"status": "success", "confirmed": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(resp_data)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"OK")

    def do_PUT(self):
        url = urllib.parse.urlsplit(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        put_data = self.rfile.read(content_length) if content_length > 0 else b""

        if "experience" in url.path:
            exp_id = url.path.split("/")[-1]
            if exp_id in self.test_server.experiences:
                try:
                    data = json.loads(put_data.decode("utf-8"))
                    self.test_server.experiences[exp_id]["company"] = data.get("company", self.test_server.experiences[exp_id]["company"])
                    self.test_server.operations_log.append({
                        "op": "EDIT",
                        "entity": "experience",
                        "id": exp_id,
                        "data": dict(self.test_server.experiences[exp_id]),
                    })
                    resp = json.dumps(self.test_server.experiences[exp_id]).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(resp)
                    return
                except Exception:
                    pass
            self.send_response(404)
            self.send_header("Connection", "close")
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Connection", "close")
        self.end_headers()

    def do_DELETE(self):
        url = urllib.parse.urlsplit(self.path)
        if "experience" in url.path:
            exp_id = url.path.split("/")[-1]
            if self.test_server.fail_delete_server or self.test_server.fail_delete_optimistically:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b'{"error": "Server database failure"}')
                return

            if exp_id in self.test_server.experiences:
                del self.test_server.experiences[exp_id]
                self.test_server.operations_log.append({
                    "op": "DELETE",
                    "entity": "experience",
                    "id": exp_id,
                })
                resp = json.dumps({"status": "deleted", "id": exp_id}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(resp)
                return
            self.send_response(404)
            self.send_header("Connection", "close")
            self.end_headers()
            return

        if "attachment" in url.path:
            if getattr(self.test_server, "fail_delete_attachment_server", False):
                status_code = getattr(self.test_server, "fail_delete_status_code", 500)
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b'{"status": "error", "message": "Simulated attachment delete failure"}')
                return
            att_id = url.path.split("/")[-1]
            if att_id in self.test_server.attachments:
                del self.test_server.attachments[att_id]
                self.test_server.operations_log.append({
                    "op": "DELETE",
                    "entity": "attachment",
                    "id": att_id,
                })
                resp = json.dumps({"status": "deleted", "id": att_id}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(resp)
                return
            self.send_response(404)
            self.send_header("Connection", "close")
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Connection", "close")
        self.end_headers()


class ProfileTestServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host="127.0.0.1", port=0):
        super().__init__((host, port), ProfileTestHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.experiences = {
            "exp-preexisting-1": {
                "id": "exp-preexisting-1",
                "company": "Nordic QA Governance (Pre-existing User Entry)",
                "role": "Principal QA Specialist",
                "desc": "2019-Present",
            }
        }
        self.attachments = {}
        self.next_exp_id = 100
        self.next_att_id = 500
        self.fail_delete_server = False
        self.fail_delete_optimistically = False
        self.operations_log = []
        self.reload_count = 0
        self.fail_readback_after_reloads = None
        self.upload_destination_contract = False
        self.fail_upload_server = False
        self.fail_upload_status_code = 500
        self.fail_delete_attachment_server = False
        self.fail_delete_status_code = 500
        self.fail_create_server = False
        self.fail_create_status_code = 500

/**
 * Safety features: protected senders, history + undo, previews,
 * and the remote ("paste the redirect address") sign-in panel.
 */

window.GmailCleaner = window.GmailCleaner || {};

GmailCleaner.api = async function (url, options = {}) {
    const response = await fetch(url, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
    });
    let data = {};
    try {
        data = await response.json();
    } catch (_) {
        /* empty body */
    }
    if (!response.ok) {
        throw new Error(data.detail || `Request failed (${response.status})`);
    }
    return data;
};

GmailCleaner.Safety = {
    protectedList: [],

    // ---------- helpers ----------

    esc(text) {
        return GmailCleaner.UI.escapeHtml(text);
    },

    /** Client-side mirror of the server rule, used only for display. */
    isProtected(sender) {
        if (!sender) return false;
        const s = String(sender).trim().toLowerCase();
        const domain = s.includes('@') ? s.split('@').pop() : s;
        return this.protectedList.some(e =>
            e.includes('@') ? s === e : domain === e || domain.endsWith('.' + e)
        );
    },

    timeAgo(iso) {
        const then = new Date(iso);
        const secs = Math.round((Date.now() - then.getTime()) / 1000);
        if (secs < 60) return 'just now';
        const mins = Math.round(secs / 60);
        if (mins < 60) return `${mins} min ago`;
        const hours = Math.round(mins / 60);
        if (hours < 24) return `${hours} h ago`;
        return then.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    },

    async refresh() {
        await Promise.all([this.loadProtected(), this.loadHistory()]);
    },

    // ---------- protected senders ----------

    async loadProtected() {
        try {
            const data = await GmailCleaner.api('/api/protected');
            this.protectedList = data.senders || [];
        } catch (e) {
            GmailCleaner.UI.showErrorToast(e.message);
        }
        this.renderProtected();
    },

    renderProtected() {
        const list = document.getElementById('protectedList');
        const count = document.getElementById('protectedCount');
        if (!list) return;
        if (count) count.textContent = this.protectedList.length;

        if (this.protectedList.length === 0) {
            list.innerHTML = `<p class="safety-empty">Nothing protected yet. Add your school, bank and anything you never want bulk-deleted.</p>`;
            return;
        }
        list.innerHTML = this.protectedList
            .map(e => `
                <span class="protected-chip">
                    <span class="protected-chip-icon">${e.includes('@') ? '@' : '◎'}</span>
                    <span>${this.esc(e)}</span>
                    <button type="button" class="protected-chip-remove" title="Remove ${this.esc(e)}"
                        data-entry="${this.esc(e)}">×</button>
                </span>`)
            .join('');
        list.querySelectorAll('.protected-chip-remove').forEach(btn => {
            btn.addEventListener('click', () => this.removeProtected(btn.dataset.entry));
        });
    },

    async addProtected(value) {
        const input = document.getElementById('protectedInput');
        const raw = (value ?? input?.value ?? '').trim();
        if (!raw) return false;
        try {
            const data = await GmailCleaner.api('/api/protected', {
                method: 'POST',
                body: JSON.stringify({ sender: raw }),
            });
            const added = data.senders.find(e => !this.protectedList.includes(e)) || raw;
            this.protectedList = data.senders;
            if (input && value === undefined) input.value = '';
            this.renderProtected();
            this.refreshResultLists();
            GmailCleaner.UI.showSuccessToast(`🛡️ ${this.esc(added)} is now protected`);
            return true;
        } catch (e) {
            GmailCleaner.UI.showErrorToast(this.esc(e.message));
            return false;
        }
    },

    async removeProtected(entry) {
        try {
            const data = await GmailCleaner.api('/api/protected/remove', {
                method: 'POST',
                body: JSON.stringify({ sender: entry }),
            });
            this.protectedList = data.senders;
            this.renderProtected();
            this.refreshResultLists();
        } catch (e) {
            GmailCleaner.UI.showErrorToast(this.esc(e.message));
        }
    },

    /** Re-flag rows in the scan result lists after the list changes. */
    refreshResultLists() {
        (GmailCleaner.deleteResults || []).forEach(r => { r.protected = this.isProtected(r.email); });
        (GmailCleaner.results || []).forEach(r => { r.protected = this.isProtected(r.domain); });
        if (GmailCleaner.Delete && (GmailCleaner.deleteResults || []).length) GmailCleaner.Delete.displayResults();
        if (GmailCleaner.Scanner && (GmailCleaner.results || []).length) GmailCleaner.Scanner.displayResults();
    },

    // ---------- history + undo ----------

    async loadHistory() {
        const list = document.getElementById('historyList');
        if (!list) return;
        try {
            const data = await GmailCleaner.api('/api/history');
            this.renderHistory(data.entries || []);
        } catch (e) {
            list.innerHTML = `<p class="safety-empty">${this.esc(e.message)}</p>`;
        }
    },

    renderHistory(entries) {
        const list = document.getElementById('historyList');
        if (!entries.length) {
            list.innerHTML = `<p class="safety-empty">No actions yet. Everything you trash, archive or mark read shows up here with an Undo button.</p>`;
            return;
        }
        const icons = { trash: '🗑️', archive: '📦', mark_read: '✉️' };
        list.innerHTML = entries.map(e => {
            const who = e.senders && e.senders.length
                ? this.esc(e.senders.slice(0, 3).join(', ')) +
                  (e.sender_count > 3 ? ` +${e.sender_count - 3} more` : '')
                : 'matching your filters';
            let right;
            if (e.undone) {
                const r = e.undo_result || {};
                right = `<span class="history-status done">↩ Undone${r.failed ? ` (${r.failed} failed)` : ''}</span>`;
            } else if (e.expired) {
                right = `<span class="history-status muted" title="Gmail empties Trash after 30 days">Expired</span>`;
            } else {
                right = `<button class="btn btn-sm btn-secondary" data-undo="${this.esc(e.id)}">↩ Undo</button>`;
            }
            return `
                <div class="history-item ${e.undone ? 'is-undone' : ''}">
                    <div class="history-icon">${icons[e.action] || '•'}</div>
                    <div class="history-body">
                        <div class="history-title">${this.esc(e.label)} <strong>${e.count.toLocaleString()}</strong> emails${e.truncated ? ' (partial undo)' : ''}</div>
                        <div class="history-sub">${who} · ${this.timeAgo(e.timestamp)}</div>
                    </div>
                    <div class="history-actions">${right}</div>
                </div>`;
        }).join('');
        list.querySelectorAll('[data-undo]').forEach(btn => {
            btn.addEventListener('click', () => this.undo(btn.dataset.undo, btn));
        });
    },

    async undo(id, btn) {
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Undoing...';
        }
        try {
            const result = await GmailCleaner.api(`/api/history/${encodeURIComponent(id)}/undo`, { method: 'POST' });
            if (result.success) {
                GmailCleaner.UI.showSuccessToast(this.esc(result.message));
                if (btn) btn.textContent = '✓ Undone';
            } else {
                GmailCleaner.UI.showErrorToast(this.esc(result.message));
                if (btn) btn.textContent = 'Undo failed';
            }
        } catch (e) {
            GmailCleaner.UI.showErrorToast(this.esc(e.message));
            if (btn) {
                btn.disabled = false;
                btn.textContent = '↩ Undo';
            }
        }
        this.loadHistory();
        if (GmailCleaner.currentView === 'markread' && GmailCleaner.MarkRead) {
            GmailCleaner.MarkRead.refreshUnreadCount();
        }
    },

    /** Success toast with an inline Undo button. */
    showUndoToast(message, historyId, skippedNote = '') {
        const tip = skippedNote ? this.esc(skippedNote) : null;
        if (!historyId) {
            GmailCleaner.UI.showToast(this.esc(message), 'success', 6000, tip);
            return;
        }
        const id = this.esc(historyId);
        GmailCleaner.UI.showToast(
            `${this.esc(message)} <button class="toast-undo" onclick="GmailCleaner.Safety.undo('${id}', this)">↩ Undo</button>`,
            'success',
            12000,
            tip
        );
    },

    skippedNote(status) {
        const parts = [];
        const senders = status.skipped_senders || [];
        if (senders.length) parts.push(`🛡️ Skipped protected: ${senders.slice(0, 3).join(', ')}${senders.length > 3 ? '…' : ''}`);
        if (status.skipped_protected) parts.push(`🛡️ ${status.skipped_protected} protected emails left alone`);
        return parts.join(' · ');
    },
};

// ===================================================================
// Preview modal
// ===================================================================

GmailCleaner.Preview = {
    copy: {
        trash: { verb: 'Move to Trash', title: 'Move to Trash?', btn: 'btn-danger', note: 'Recoverable for 30 days, and you can undo it from the Safety page.' },
        archive: { verb: 'Archive', title: 'Archive these emails?', btn: 'btn-primary', note: 'They leave your inbox but stay in All Mail. Undo puts them back.' },
        mark_read: { verb: 'Mark as read', title: 'Mark these as read?', btn: 'btn-primary', note: 'Undo marks them unread again.' },
    },

    close(result) {
        const overlay = document.getElementById('previewOverlay');
        if (overlay) overlay.remove();
        document.removeEventListener('keydown', this._onKey);
        if (this._resolve) {
            const resolve = this._resolve;
            this._resolve = null;
            resolve(result);
        }
    },

    /**
     * Show a dry-run preview and resolve true if the user confirms.
     * opts: { action: 'trash'|'archive'|'mark_read', senders?, count?, filters? }
     */
    confirm(opts) {
        this.close(false);
        const c = this.copy[opts.action];
        const esc = GmailCleaner.UI.escapeHtml;

        const overlay = document.createElement('div');
        overlay.id = 'previewOverlay';
        overlay.className = 'preview-overlay';
        overlay.innerHTML = `
            <div class="preview-modal" role="dialog" aria-modal="true" aria-labelledby="previewTitle">
                <h3 id="previewTitle">${c.title}</h3>
                <div class="preview-body" id="previewBody">
                    <div class="preview-loading">
                        <svg class="spinner" viewBox="0 0 24 24" width="28" height="28"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="2" stroke-dasharray="60" stroke-linecap="round"/></svg>
                        <span>Counting exactly what this will touch...</span>
                    </div>
                </div>
                <div class="preview-footer">
                    <button class="btn btn-secondary" id="previewCancel">Cancel</button>
                    <button class="btn ${c.btn}" id="previewConfirm" disabled>${c.verb}</button>
                </div>
            </div>`;
        document.body.appendChild(overlay);

        overlay.addEventListener('click', e => { if (e.target === overlay) this.close(false); });
        document.getElementById('previewCancel').addEventListener('click', () => this.close(false));
        document.getElementById('previewConfirm').addEventListener('click', () => this.close(true));
        this._onKey = e => { if (e.key === 'Escape') this.close(false); };
        document.addEventListener('keydown', this._onKey);

        const promise = new Promise(resolve => { this._resolve = resolve; });

        GmailCleaner.api('/api/preview', {
            method: 'POST',
            body: JSON.stringify({
                action: opts.action,
                senders: opts.senders || [],
                count: opts.count || 0,
                filters: opts.filters || null,
            }),
        }).then(p => {
            if (!document.getElementById('previewOverlay')) return;
            const body = document.getElementById('previewBody');
            const confirmBtn = document.getElementById('previewConfirm');
            if (!p.success) {
                body.innerHTML = `<p class="preview-error">${esc(p.message)}</p>`;
                return;
            }

            const skipped = (p.skipped_senders || []).length
                ? `<div class="preview-protected">🛡️ <strong>Protected, will be skipped:</strong> ${p.skipped_senders.map(esc).join(', ')}</div>`
                : '';
            const perSender = (p.per_sender || []).length > 1
                ? `<div class="preview-section-title">By sender</div>
                   <ul class="preview-senders">
                     ${p.per_sender.slice(0, 8).map(r => `<li><span>${esc(r.sender)}</span><span>${r.count.toLocaleString()}</span></li>`).join('')}
                     ${p.per_sender.length > 8 ? `<li class="muted"><span>+ ${p.per_sender.length - 8} more senders</span><span></span></li>` : ''}
                   </ul>`
                : '';
            const samples = (p.samples || []).length
                ? `<div class="preview-section-title">Sample emails</div>
                   <ul class="preview-samples">
                     ${p.samples.map(s => `<li><div class="preview-sample-subject">${esc(s.subject)}</div><div class="preview-sample-from">${esc(s.from)}</div></li>`).join('')}
                   </ul>`
                : '';

            if (p.total === 0) {
                body.innerHTML = `${skipped}<p class="preview-zero">Nothing to do: no matching emails${skipped ? ' outside your protected list' : ''}.</p>`;
                confirmBtn.remove();
                document.getElementById('previewCancel').textContent = 'Close';
                return;
            }

            body.innerHTML = `
                <div class="preview-total"><span>${p.total.toLocaleString()}</span> emails</div>
                ${skipped}
                ${perSender}
                ${samples}
                <p class="preview-note">${c.note}</p>`;
            confirmBtn.textContent = `${c.verb} (${p.total.toLocaleString()})`;
            confirmBtn.disabled = false;
            confirmBtn.focus();
        }).catch(e => {
            const body = document.getElementById('previewBody');
            if (body) body.innerHTML = `<p class="preview-error">${esc(e.message)}</p>`;
        });

        return promise;
    },
};

// ===================================================================
// Remote sign-in (app running on a server)
// ===================================================================

GmailCleaner.RemoteSignIn = {
    async show() {
        const panel = document.getElementById('remoteSignIn');
        const link = document.getElementById('remoteAuthLink');
        if (!panel) return;
        panel.classList.remove('hidden');
        link.classList.add('disabled');
        link.textContent = 'Preparing Google link...';

        for (let i = 0; i < 20; i++) {
            try {
                const s = await GmailCleaner.api('/api/web-auth-status');
                if (s.pending_auth_url) {
                    link.href = s.pending_auth_url;
                    link.classList.remove('disabled');
                    link.textContent = 'Open Google sign-in ↗';
                    return;
                }
            } catch (_) { /* keep trying */ }
            await new Promise(r => setTimeout(r, 500));
        }
        link.textContent = 'Could not get the Google link. Try Sign in again.';
    },

    hide() {
        const panel = document.getElementById('remoteSignIn');
        if (panel) panel.classList.add('hidden');
    },

    async submit() {
        const input = document.getElementById('oauthPasteInput');
        const msg = document.getElementById('oauthPasteMsg');
        const url = (input.value || '').trim();
        if (!url) return;
        msg.className = 'remote-msg';
        msg.textContent = 'Checking...';
        try {
            const r = await GmailCleaner.api('/api/oauth-paste', {
                method: 'POST',
                body: JSON.stringify({ url }),
            });
            msg.className = 'remote-msg ' + (r.success ? 'ok' : 'bad');
            msg.textContent = r.message;
            if (r.success) input.value = '';
        } catch (e) {
            msg.className = 'remote-msg bad';
            msg.textContent = e.message;
        }
    },
};

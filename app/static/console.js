(() => {
    const body = document.body;
    const kind = body.dataset.consoleKind;
    const views = kind === 'developer' ? {
        overview: ['Overview', 'A clear view of everything you’re building.'],
        models: ['Model library', 'The right intelligence for every part of your workflow.'],
        api: ['API credentials', 'One connection. Your tools, your workflow.'],
        usage: ['Usage analytics', 'Understand your requests, tokens, and usage over time.'],
        wallet: ['Wallet & billing', 'Transparent costs. A balance you control.'],
    } : {
        overview: ['Overview', 'Your platform at a glance. Everything in its place.'],
        members: ['Members', 'Manage accounts, access, and prepaid balances.'],
        ledger: ['Wallet ledger', 'A transparent record of every credit and charge.'],
        reconciliation: ['Reconciliation', 'Verify stored balances against your financial ledger.'],
        payments: ['Payments', 'Verified top-ups and their payment references.'],
        settings: ['System settings', 'Fine-tune your gateway. Changes take effect immediately.'],
    };
    const hashViews = { 'model-library': 'models', credentials: 'api', 'usage-analytics': 'usage', 'wallet-activity': 'wallet' };
    const sidebar = document.getElementById('console-sidebar');
    const toggle = document.querySelector('.console-menu-toggle');
    const scrim = document.querySelector('.console-scrim');
    const mobileNavigation = window.matchMedia('(max-width: 850px)');
    function updateSidebarAccessibility() { sidebar.inert = mobileNavigation.matches && !body.classList.contains('navigation-open'); }
    mobileNavigation.addEventListener('change', () => { closeNavigation(); updateSidebarAccessibility(); });
    updateSidebarAccessibility();
    let toastTimeout;
    window.consoleNotify = message => {
        const toast = document.getElementById('console-toast');
        if (!toast) return;
        toast.textContent = message;
        toast.hidden = false;
        clearTimeout(toastTimeout);
        toastTimeout = setTimeout(() => { toast.hidden = true; }, 6000);
    };
    document.querySelectorAll('[data-copy-bank-ref]').forEach(button => button.addEventListener('click', async () => {
        const text = button.dataset.copyBankRef;
        try {
            if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
            else {
                const field = document.createElement('textarea');
                field.value = text;
                field.style.cssText = 'position:fixed;opacity:0;pointer-events:none;';
                document.body.append(field);
                try {
                    field.select();
                    if (!document.execCommand('copy')) throw new Error('Copy failed');
                } finally { field.remove(); button.focus(); }
            }
            window.consoleNotify('Bank reference copied.');
        } catch { window.consoleNotify('Could not copy. Select the bank reference and copy it manually.'); }
    }));
    window.consoleActionDialog = options => new Promise(resolve => {
        const dialog = document.getElementById('console-action-dialog');
        if (dialog.open) { resolve(null); return; }
        const form = document.getElementById('console-action-form');
        const amount = document.getElementById('console-dialog-amount');
        const note = document.getElementById('console-dialog-note');
        const confirm = document.getElementById('console-dialog-confirm');
        const adjustmentTypes = form.querySelectorAll('input[name="adjustment_type"]');
        const amountHelp = document.getElementById('console-dialog-amount-help');
        const previousFocus = document.activeElement;
        document.getElementById('console-dialog-title').textContent = options.title;
        document.getElementById('console-dialog-description').textContent = options.description;
        document.getElementById('console-dialog-fields').hidden = !options.fields;
        document.getElementById('console-dialog-member').hidden = !options.member;
        document.getElementById('console-dialog-member-name').textContent = options.member || '';
        adjustmentTypes.forEach(input => { input.checked = input.value === 'credit'; });
        amountHelp.textContent = 'Enter a positive amount to add to this wallet.';
        adjustmentTypes.forEach(input => { input.onchange = () => {
            amountHelp.textContent = input.value === 'debit' ? 'Enter a positive amount to deduct from this wallet.' : 'Enter a positive amount to add to this wallet.';
        }; });
        amount.required = Boolean(options.fields);
        amount.value = ''; amount.setCustomValidity(''); note.value = '';
        amount.oninput = () => amount.setCustomValidity('');
        confirm.textContent = options.confirm;
        confirm.classList.toggle('danger', Boolean(options.danger));
        confirm.classList.toggle('primary', !options.danger);
        let result = null;
        form.onsubmit = event => {
            event.preventDefault();
            if (options.fields && (!Number.isFinite(Number(amount.value)) || Number(amount.value) <= 0)) {
                amount.setCustomValidity('Enter a USD amount greater than zero.'); amount.reportValidity(); return;
            }
            const isDebit = form.querySelector('input[name="adjustment_type"]:checked').value === 'debit';
            result = options.fields ? { amount: Number(amount.value) * (isDebit ? -1 : 1), note: note.value.trim() } : true;
            dialog.close();
        };
        document.getElementById('console-dialog-cancel').onclick = () => dialog.close();
        dialog.addEventListener('close', () => { if (previousFocus?.isConnected) previousFocus.focus(); resolve(result); }, { once: true });
        dialog.showModal();
        (options.fields ? amount : document.getElementById('console-dialog-cancel')).focus();
    });
    function closeNavigation() {
        body.classList.remove('navigation-open');
        toggle.setAttribute('aria-expanded', 'false');
        scrim.hidden = true;
        updateSidebarAccessibility();
    }
    window.consoleCloseNavigation = closeNavigation;
    toggle.addEventListener('click', event => {
        const open = !body.classList.contains('navigation-open');
        body.classList.toggle('navigation-open', open);
        toggle.setAttribute('aria-expanded', String(open));
        scrim.hidden = !open;
        updateSidebarAccessibility();
        // Only move focus into the sidebar for keyboard activation (detail === 0);
        // pointer/touch opens should not paint the :focus-visible ring.
        if (open && event.detail === 0) sidebar.querySelector('a').focus();
    });
    scrim.addEventListener('click', () => { closeNavigation(); toggle.focus(); });
    document.addEventListener('keydown', event => {
        if (!body.classList.contains('navigation-open')) return;
        if (event.key === 'Escape') { closeNavigation(); toggle.focus(); }
        if (event.key === 'Tab') {
            const items = [...sidebar.querySelectorAll('a,button')];
            const first = items[0], last = items[items.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
        }
    });
    if (kind !== 'audit') {
        function showView() {
            const hash = location.hash.slice(1);
            const candidate = hashViews[hash] || hash || 'overview';
            const active = Object.hasOwn(views, candidate) ? candidate : 'overview';
            body.dataset.activeView = active;
            body.classList.remove('console-booting');
            document.querySelectorAll('[data-console-view]').forEach(panel => {
                panel.hidden = !panel.dataset.consoleView.split(' ').includes(active);
            });
            const emptyModels = document.getElementById('model-search-empty');
            if (emptyModels) emptyModels.hidden = active !== 'models' || [...document.querySelectorAll('[data-library-model]')].some(row => !row.hidden);
            document.querySelectorAll('[data-console-nav]').forEach(link => {
                const selected = link.dataset.consoleNav === active;
                link.classList.toggle('is-active', selected);
                if (selected) link.setAttribute('aria-current', 'page');
                else link.removeAttribute('aria-current');
            });
            document.querySelector('[data-console-title]').textContent = views[active][0];
            document.querySelector('[data-console-description]').textContent = views[active][1];
            document.querySelector('[data-console-breadcrumb]').textContent = views[active][0];
            document.title = `${views[active][0]} | AI Gateway`;
            closeNavigation();
            window.scrollTo({ top: 0, behavior: 'instant' });
            requestAnimationFrame(() => {
                if (window.Chart) document.querySelectorAll('canvas').forEach(canvas => {
                    const chart = Chart.getChart(canvas);
                    if (chart && !canvas.closest('[hidden]')) chart.resize();
                });
            });
        }
        window.addEventListener('hashchange', showView);
        showView();
    }
    document.querySelectorAll('[data-console-nav]').forEach(link => link.addEventListener('click', () => closeNavigation()));
    const memberSearch = document.getElementById('member-search');
    if (memberSearch) memberSearch.addEventListener('input', () => {
        const query = memberSearch.value.trim().toLowerCase();
        let count = 0;
        document.querySelectorAll('[data-member-search]').forEach(row => {
            row.hidden = !row.dataset.memberSearch.toLowerCase().includes(query);
            if (!row.hidden) count++;
        });
        document.getElementById('member-search-empty').hidden = count !== 0 || !query;
        document.getElementById('member-result-count').textContent = `${count} members`;
    });
    const modelSearch = document.getElementById('console-model-search');
    const modelTier = document.getElementById('console-model-tier');
    function filterLibrary() {
        const query = modelSearch.value.trim().toLowerCase();
        const tier = modelTier.value;
        let count = 0;
        document.querySelectorAll('[data-library-model]').forEach(row => {
            row.hidden = !row.dataset.libraryModel.toLowerCase().includes(query) || (tier !== 'all' && row.dataset.libraryTier !== tier);
            if (!row.hidden) count++;
        });
        document.getElementById('model-search-empty').hidden = count !== 0;
        document.getElementById('model-result-count').textContent = `${count} models`;
    }
    if (modelSearch) { modelSearch.addEventListener('input', filterLibrary); modelTier.addEventListener('change', filterLibrary); }
    // Premium margin reconciliation (read-only; admin only).
    const reconcileRoot = document.querySelector('[data-reconcile-root]');
    if (reconcileRoot) {
        const runBtn = reconcileRoot.querySelector('[data-reconcile-run]');
        const daysSel = reconcileRoot.querySelector('[data-reconcile-days]');
        const out = reconcileRoot.querySelector('[data-reconcile-result]');
        const epochBtn = reconcileRoot.querySelector('[data-reconcile-epoch]');
        const epochNote = reconcileRoot.querySelector('[data-reconcile-epoch-note]');
        const usd = n => '$' + Number(n || 0).toFixed(6);
        const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
        const cell = (label, value, cls) => `<div class="px-3 py-1"><div class="text-[11px] uppercase tracking-wider text-zinc-400">${label}</div><div class="font-mono ${cls || 'text-zinc-200'}">${value}</div></div>`;
        const showEpoch = epoch => {
            if (!epochNote) return;
            epochNote.innerHTML = epoch
                ? `Measuring fresh since <span class="font-mono text-zinc-300">${esc(epoch)}</span> — older requests are kept but not counted.`
                : 'Measuring from the start of the selected window (no fresh epoch set).';
        };
        const render = d => {
            const p = d.portal;
            const m = d.margin || {};
            const cells = [];
            cells.push(cell('Billed (customers)', usd(p.billed_usd), 'text-emerald-400'));
            const measured = (m.measured_cost_usd !== undefined && m.measured_cost_usd !== null) ? m.measured_cost_usd : p.observed_upstream_usd;
            cells.push(cell('Cost (measured)', usd(measured), 'text-amber-300'));
            if (d.upstream) {
                const bg = m.business_gross_usd;
                const hasBg = (bg !== undefined && bg !== null);
                cells.push(cell('Business margin', hasBg ? `${usd(bg)}${m.business_ratio ? ` · ${m.business_ratio}x` : ''}` : '—', hasBg ? (bg >= 0 ? 'text-emerald-400' : 'text-rose-400') : 'text-zinc-500'));
            } else {
                cells.push(cell('Business margin', '—', 'text-zinc-500'));
            }
            const notes = [];
            if (d.account) {
                const pending = d.account.fiat_pending_usdc > 0
                    ? ` · <span class="text-zinc-500">pending</span> <span class="font-mono text-zinc-300">${usd(d.account.fiat_pending_usdc)}</span>`
                    : '';
                notes.push(`InferHub balance: <span class="font-mono text-emerald-400">${usd(d.account.balance_usdc)}</span>${pending}`);
            } else if (d.account_error) {
                notes.push(`<span class="text-zinc-500">InferHub balance unavailable: ${esc(d.account_error)}</span>`);
            }
            notes.push(`Paid margin: <span class="font-mono text-zinc-300">${usd(m.paid_gross_usd || 0)}</span> · Trial cost (free): <span class="font-mono text-zinc-300">${usd(m.trial_cost_usd || 0)}</span>`);
            notes.push(`Portal premium requests: <span class="font-mono text-zinc-300">${p.requests}</span> · tokens in/out/cached: <span class="font-mono text-zinc-300">${p.tokens_in}/${p.tokens_out}/${p.tokens_cached}</span>`);
            if (d.upstream) {
                const gap = m.observed_vs_upstream_gap_usd || 0;
                const gapCls = Math.abs(gap) > 0.01 ? 'text-amber-300' : 'text-zinc-400';
                notes.push(`Cross-check — <span class="text-zinc-300">whole account</span> spent <span class="font-mono text-zinc-300">${usd(d.upstream.cost_usdc)}</span> <span class="text-zinc-500">(all keys · full window, not just the portal)</span> · <span class="${gapCls}">gap vs measured ${usd(gap)}</span>`);
                if (d.measurement && d.measurement.epoch_after_window_start) {
                    notes.push(`<span class="text-amber-300">⚠ The portal side counts only since the measurement epoch (${esc(d.measurement.effective_from_local)}), but the account total covers the whole window — so most of the gap is just that time mismatch, not a leak. Read it as a rough signal only.</span>`);
                }
            }
            if (p.estimated_requests > 0) {
                const detail = p.estimated_cost_usd > 0
                    ? `priced at the measured average (${usd(p.estimated_cost_usd)})`
                    : 'unverifiable usage (never billed)';
                notes.push(`<span class="text-amber-300">${p.estimated_requests} estimated request(s) — ${detail}.</span>`);
            }
            if (d.upstream_error) {
                notes.push(`<span class="text-rose-400">Upstream usage unavailable: ${esc(d.upstream_error)}</span>`);
            }
            out.innerHTML = `<div class="grid grid-cols-3 divide-x divide-zinc-800/80 bg-zinc-950/70 rounded-lg border border-zinc-800/80 py-2 shadow-inner">${cells.join('')}</div>`
                + `<div class="mt-3 space-y-1 text-[11px] text-zinc-400">${notes.map(n => `<div>${n}</div>`).join('')}</div>`;
            if (d.measurement) showEpoch(d.measurement.epoch);
        };
        const run = async () => {
            runBtn.disabled = true;
            out.textContent = 'Running reconciliation…';
            try {
                const res = await fetch(`/api/admin/reconcile?days=${encodeURIComponent(daysSel.value)}`, { headers: { Accept: 'application/json' } });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const { data } = await res.json();
                render(data);
            } catch (err) {
                out.innerHTML = `<span class="text-rose-400">Reconciliation failed: ${esc(err.message)}</span>`;
            } finally { runBtn.disabled = false; }
        };
        runBtn.addEventListener('click', run);
        if (epochBtn) {
            epochBtn.addEventListener('click', async () => {
                if (!window.confirm('Start measuring fresh from now?\n\nOld requests are kept but excluded from the margin. This only affects the report, never billing.')) return;
                epochBtn.disabled = true;
                try {
                    const res = await fetch('/api/admin/metrics-epoch', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                        body: JSON.stringify({ action: 'start' }),
                    });
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    const { epoch } = await res.json();
                    showEpoch(epoch);
                    await run();
                } catch (err) {
                    out.innerHTML = `<span class="text-rose-400">Could not start a fresh measurement: ${esc(err.message)}</span>`;
                } finally { epochBtn.disabled = false; }
            });
        }
    }
    // Associate legacy form labels without changing field names or API contracts.
    document.querySelectorAll('.console-app label').forEach((label, index) => {
        if (label.htmlFor || label.querySelector('input')) return;
        const field = label.parentElement.querySelector('input,select,textarea');
        if (!field) return;
        if (!field.id) field.id = `console-field-${index}`;
        label.htmlFor = field.id;
    });
})();

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
        costs: ['Costs & margin', 'Understand premium revenue, provider costs, and the margin in between.'],
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
        const status = reconcileRoot.querySelector('[data-reconcile-status]');
        const summaryNote = document.querySelector('[data-margin-summary-note]');
        let busy = false;
        const usd = n => '$' + Number(n || 0).toFixed(6);
        const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
        const count = n => Number(n || 0).toLocaleString('en-US');
        const cell = (label, value, note, cls = '') => `<article class="margin-card ${cls}"><span>${label}</span><strong>${value}</strong><p>${note}</p></article>`;
        const stat = (label, value, note = '') => `<div><dt>${label}</dt><dd>${value}</dd>${note ? `<small>${note}</small>` : ''}</div>`;
        const setBusy = value => {
            busy = value;
            runBtn.disabled = daysSel.disabled = epochBtn.disabled = value;
            runBtn.innerHTML = value ? '<i class="fa-solid fa-rotate fa-spin" aria-hidden="true"></i>Refreshing…' : '<i class="fa-solid fa-rotate" aria-hidden="true"></i>Refresh report';
            out.setAttribute('aria-busy', String(value));
        };
        const showEpoch = epoch => {
            if (!epochNote) return;
            epochNote.textContent = epoch
                ? `Measuring since ${epoch} — older requests are kept but not counted.`
                : 'Measuring from the start of the selected window.';
        };
        const render = d => {
            const p = d.portal;
            const m = d.margin || {};
            const measured = (m.measured_cost_usd !== undefined && m.measured_cost_usd !== null) ? m.measured_cost_usd : p.observed_upstream_usd;
            const bg = m.business_gross_usd;
            const hasBg = bg !== undefined && bg !== null;
            const hasMargin = Object.hasOwn(m, 'paid_gross_usd');
            const marginClass = hasBg ? (bg >= 0 ? 'margin-positive' : 'margin-negative') : '';
            const cards = [
                cell('Customer revenue', usd(p.billed_usd), 'Settled premium usage', 'margin-positive'),
                cell('Measured upstream cost', usd(measured), 'Paid usage and free trials', 'margin-cost'),
                cell('Gross margin', hasBg ? usd(bg) : '—', 'Revenue minus measured cost · before overhead', `margin-card-featured ${marginClass}`),
            ];
            const period = daysSel.selectedOptions[0].textContent;
            document.querySelectorAll('[data-margin-summary]').forEach(el => {
                const key = el.dataset.marginSummary;
                el.textContent = { revenue: usd(p.billed_usd), cost: usd(measured), margin: hasBg ? usd(bg) : '—' }[key];
                el.classList.toggle('margin-negative', key === 'margin' && hasBg && bg < 0);
                el.classList.toggle('margin-positive', key === 'margin' && hasBg && bg >= 0);
            });
            summaryNote.textContent = `${period} · ${count(p.requests)} premium requests${d.measurement?.epoch_after_window_start ? ' · Measurement start limits this period' : ''}${d.upstream_error ? ' · Upstream unavailable' : ''}`;
            const notes = [];
            const accountStats = [];
            if (d.account) {
                accountStats.push(stat('InferHub balance', usd(d.account.balance_usdc), 'Current account balance'));
                accountStats.push(stat('Pending funds', usd(d.account.fiat_pending_usdc), 'Not yet settled'));
            } else if (d.account_error) {
                notes.push(`<p>InferHub balance unavailable: ${esc(d.account_error)}</p>`);
            }
            let warning = '';
            if (d.upstream) {
                const gap = m.observed_vs_upstream_gap_usd || 0;
                accountStats.push(stat('Whole-account spend', usd(d.upstream.cost_usdc), 'All keys · full selected window'));
                accountStats.push(stat('Observed cost − account spend', usd(gap), 'Cross-check only · not profit or loss'));
                notes.push('<p>Account spend includes every key, not just portal traffic. This difference is a rough diagnostic signal, not a business margin.</p>');
                if (d.measurement && d.measurement.epoch_after_window_start) {
                    warning = '<div class="margin-notice"><i class="fa-solid fa-circle-info" aria-hidden="true"></i><div><strong>Different measurement windows</strong><p>The account cross-check covers a longer period. Its difference is not profit, loss, or proof of a leak. Expand provider cross-check for details.</p></div></div>';
                    notes.push(`<p>Portal usage starts at ${esc(d.measurement.effective_from_local)}; account spend covers the whole selected window.</p>`);
                }
            }
            if (p.estimated_requests > 0) {
                const detail = p.estimated_cost_usd > 0
                    ? `priced at the measured average (${usd(p.estimated_cost_usd)})`
                    : 'unverifiable usage (never billed)';
                warning += `<div class="margin-notice"><i class="fa-solid fa-circle-info" aria-hidden="true"></i><div><strong>Includes estimated usage</strong><p>${count(p.estimated_requests)} request(s) — ${detail}.</p></div></div>`;
            }
            if (d.upstream_error) {
                warning += '<div class="margin-notice"><i class="fa-solid fa-circle-info" aria-hidden="true"></i><div><strong>Provider usage unavailable</strong><p>Portal usage is still shown. Gross margin and trial cost are unavailable until the provider check succeeds.</p></div></div>';
                notes.push(`<p>Upstream usage unavailable: ${esc(d.upstream_error)}</p>`);
            }
            const detailsOpen = out.querySelector('details')?.open;
            out.innerHTML = `<div class="margin-cards">${cards.join('')}</div>${warning}
                <section class="margin-usage"><div class="margin-section-heading"><h2>Premium usage</h2><span>${esc(period)} · portal traffic only</span></div>
                <dl class="margin-usage-grid">${stat('Requests', count(p.requests))}${stat('Input tokens', count(p.tokens_in))}${stat('Output tokens', count(p.tokens_out))}${stat('Cached input tokens', count(p.tokens_cached), 'Reported cache reads · not a price')}</dl>
                <dl class="margin-breakdown">${stat('Paid usage margin', hasMargin ? usd(m.paid_gross_usd) : '—', 'Excludes free-trial cost')}${stat('Free-trial cost', hasMargin ? usd(m.trial_cost_usd) : '—', 'Provider cost paid by the platform')}</dl></section>
                <details class="margin-details" ${detailsOpen ? 'open' : ''}><summary><i class="fa-solid fa-scale-balanced" aria-hidden="true"></i>Provider cross-check<span>Account balances &amp; diagnostics</span></summary>
                <div class="margin-details-body"><dl class="margin-account-grid">${accountStats.join('')}</dl><div class="margin-diagnostics">${notes.join('')}</div>
                <p>Account window (UTC): ${esc(d.from_utc)} → ${esc(d.to_utc)}</p></div></details>`;
            if (d.measurement) showEpoch(d.measurement.epoch);
        };
        const run = async () => {
            if (busy) return;
            setBusy(true);
            status.textContent = 'Refreshing report…';
            try {
                const res = await fetch(`/api/admin/reconcile?days=${encodeURIComponent(daysSel.value)}`, { headers: { Accept: 'application/json' } });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const { data } = await res.json();
                render(data);
                status.textContent = `Updated ${new Date().toLocaleTimeString()} · All amounts in USD`;
            } catch (err) {
                status.textContent = `Report refresh failed: ${err.message}. Retry with Refresh report. Any displayed values are from the previous update.`;
                summaryNote.textContent = 'Report unavailable or out of date. Open details to retry.';
            } finally { setBusy(false); }
        };
        runBtn.addEventListener('click', run);
        daysSel.addEventListener('change', run);
        if (epochBtn) {
            epochBtn.addEventListener('click', async () => {
                if (busy) return;
                const confirmed = await window.consoleActionDialog({ title: 'Start measuring fresh?', description: 'Count report usage only from now. Older requests remain in history but are excluded from the margin report. Wallet balances and customer billing will not change.', confirm: 'Start fresh measurement' });
                if (!confirmed || busy) return;
                setBusy(true);
                try {
                    const res = await fetch('/api/admin/metrics-epoch', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                        body: JSON.stringify({ action: 'start' }),
                    });
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    const { epoch } = await res.json();
                    showEpoch(epoch);
                    setBusy(false);
                    await run();
                } catch (err) {
                    status.textContent = `Could not start a fresh measurement: ${err.message}`;
                } finally { setBusy(false); }
            });
        }
        run();
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

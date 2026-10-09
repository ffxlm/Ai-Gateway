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
    // Associate legacy form labels without changing field names or API contracts.
    document.querySelectorAll('.console-app label').forEach((label, index) => {
        if (label.htmlFor || label.querySelector('input')) return;
        const field = label.parentElement.querySelector('input,select,textarea');
        if (!field) return;
        if (!field.id) field.id = `console-field-${index}`;
        label.htmlFor = field.id;
    });
})();

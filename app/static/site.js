(() => {
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if ('IntersectionObserver' in window && !reducedMotion) {
        const observer = new IntersectionObserver(entries => {
            entries.forEach(entry => {
                if (entry.isIntersecting) {
                    entry.target.classList.add('is-visible');
                    observer.unobserve(entry.target);
                }
            });
        }, { threshold: 0.08 });
        document.querySelectorAll('.reveal').forEach(element => {
            observer.observe(element);
        });
    }
    const tabs = [...document.querySelectorAll('[data-code-tab]')];
    function selectTab(tab) {
        tabs.forEach(item => {
            const selected = item === tab;
            item.setAttribute('aria-selected', String(selected));
            item.tabIndex = selected ? 0 : -1;
            document.getElementById(`${item.dataset.codeTab}-example`).hidden = !selected;
        });
    }
    tabs.forEach((tab, index) => {
        tab.addEventListener('click', () => selectTab(tab));
        tab.addEventListener('keydown', event => {
            let target;
            if (event.key === 'ArrowRight') target = tabs[(index + 1) % tabs.length];
            if (event.key === 'ArrowLeft') target = tabs[(index - 1 + tabs.length) % tabs.length];
            if (event.key === 'Home') target = tabs[0];
            if (event.key === 'End') target = tabs[tabs.length - 1];
            if (target) { event.preventDefault(); selectTab(target); target.focus(); }
        });
    });
    const copy = document.querySelector('[data-copy-code]');
    if (copy) copy.addEventListener('click', async () => {
        const text = document.querySelector('[role="tabpanel"]:not([hidden])').textContent;
        const status = document.getElementById('copy-status');
        try {
            if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
            else {
                const field = document.createElement('textarea');
                field.value = text;
                field.style.position = 'fixed';
                field.style.opacity = '0';
                document.body.appendChild(field);
                field.select();
                const success = document.execCommand('copy');
                field.remove();
                copy.focus();
                if (!success) throw new Error('Copy unavailable');
            }
            copy.textContent = 'Copied ✓';
            status.textContent = 'Code copied to clipboard.';
        } catch {
            copy.textContent = 'Select code to copy';
            status.textContent = 'Could not copy. Select the code and copy it manually.';
        }
        window.setTimeout(() => { copy.textContent = 'Copy ⧉'; }, 2400);
    });
})();

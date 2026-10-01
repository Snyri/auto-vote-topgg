"""Shared DOM rules for observing and selecting the actual Vote control."""

VOTE_CONTROL_JS = r"""
const voteControl = (() => {
    const visible = node => {
        if (!node || !(node.getClientRects().length || node.offsetWidth || node.offsetHeight)) return false;
        for (let current = node; current; current = current.parentElement || current.getRootNode?.()?.host) {
            const style = getComputedStyle(current);
            if (style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility) ||
                Number(style.opacity) === 0) return false;
        }
        return true;
    };
    const action = node => (node.textContent || '').trim().toLowerCase() === 'vote' &&
        node.matches('button, [role="button"]') &&
        (node.tagName.toLowerCase() !== 'a' || !(node.getAttribute('href') || '').trim() ||
            (node.getAttribute('href') || '').trim().startsWith('#'));
    const disabled = node => Boolean(node.disabled || node.hasAttribute('disabled') ||
        node.matches(':disabled') || node.closest('[inert], [aria-disabled="true"]'));
    const candidates = () => [...document.querySelectorAll('button, [role="button"]')]
        .filter(node => visible(node) && action(node));
    const enabled = node => visible(node) && action(node) && !disabled(node);
    const position = (node, scroll = false) => {
        if (!visible(node) || getComputedStyle(node).pointerEvents === 'none')
            return {ready: false, reason: 'hidden'};
        if (scroll) node.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
        const rect = node.getBoundingClientRect();
        const left = Math.max(0, rect.left), right = Math.min(innerWidth, rect.right);
        const top = Math.max(0, rect.top), bottom = Math.min(innerHeight, rect.bottom);
        if (right <= left || bottom <= top) return {ready: false, reason: 'offscreen'};
        const x = (left + right) / 2, y = (top + bottom) / 2;
        const hit = document.elementFromPoint(x, y);
        if (!hit || !node.contains(hit)) return {ready: false, reason: 'covered'};
        return {ready: true, x, y, rect};
    };
    return {visible, action, disabled, candidates, enabled, position};
})();
"""

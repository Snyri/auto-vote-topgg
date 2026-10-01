"""Shared DOM observations for challenge detection and vote confirmation."""

from vote_controls import VOTE_CONTROL_JS

# Only observe existing page state. Never create or return verification tokens.
CHALLENGE_JS = VOTE_CONTROL_JS + r"""
const challengeState = (() => {
    const body = (document.body?.innerText || '').toLowerCase();
    const title = (document.title || '').trim().toLowerCase();
    const visible = voteControl.visible;
    const vote = voteControl.candidates().some(voteControl.enabled);
    const surface = vote || body.includes('vote again in') || body.includes('already voted') ||
        body.includes('thanks for voting') || body.includes('you will be able to vote after this ad');
    const groups = [
        ['iframe[src*="challenges.cloudflare.com"], .cf-turnstile',
         'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"], input[name="cf_challenge_response"]'],
        ['iframe[src*="hcaptcha.com"], .h-captcha',
         'input[name="h-captcha-response"], textarea[name="h-captcha-response"]'],
        ['iframe[src*="recaptcha"], .g-recaptcha',
         'input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]']
    ];
    let solved = false, activeWidget = false;
    for (const [selector, fields] of groups) {
        const widgets = [...document.querySelectorAll(selector)].filter(visible);
        // A wrapper and its iframe describe one widget, not two controls.
        const roots = widgets.filter(node => !widgets.some(other => other !== node && other.contains?.(node)));
        const responses = [...document.querySelectorAll(fields)].filter(field => field.value?.length > 10).length;
        const completed = roots.filter(node => node.dataset?.response?.length > 10).length;
        solved ||= responses > 0 || completed > 0;
        activeWidget ||= roots.length > Math.max(responses, completed);
    }
    const gate = [...document.querySelectorAll('#challenge-running, #challenge-stage, #challenge-form')].some(visible);
    const hard = title.startsWith('attention required');
    const managed = (typeof providerMarked !== 'undefined' && providerMarked) || title.startsWith('just a moment') ||
        body.includes('performing security verification') || body.includes('needs to review the security of your connection');
    const human = ['verify you are human', 'complete the captcha', 'please solve the captcha to continue',
        'let us know you are human'].some(marker => body.includes(marker));
    return {present: gate || (managed && !surface) || activeWidget || (human && !solved),
            solved: solved && !activeWidget && !gate, managed: gate || ((managed || hard) && !surface), visible};
})();
"""

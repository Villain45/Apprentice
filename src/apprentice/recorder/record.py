"""Apprentice recorder: logs semantic info + screenshots for each user action."""
import argparse, json, time
from pathlib import Path
from playwright.sync_api import Error as PlaywrightError, sync_playwright

SENSITIVE_SELECTOR = (
    'input[type=password], [data-sensitive], [data-apprentice-sensitive], '
    'input[autocomplete~=current-password], input[autocomplete~=new-password], '
    'input[autocomplete~=one-time-code], input[autocomplete~=cc-number], input[autocomplete~=cc-csc]'
)


def take_screenshot(page, path):
    page.screenshot(path=str(path), mask=[page.locator(SENSITIVE_SELECTOR)], mask_color="#000000")


def capture_state(page):
    try:
        return page.evaluate("() => window.__apprenticeState ? window.__apprenticeState() : null")
    except PlaywrightError:
        # Closing/navigating can destroy the execution context; the events still matter.
        return None

INJECT = r"""
(() => {
  if (window.__apprenticeInstalled) return;
  window.__apprenticeInstalled = true;

  const labelText = label => {
    const copy = label.cloneNode(true);
    copy.querySelectorAll('input, select, textarea, [contenteditable], [data-sensitive], [data-apprentice-sensitive]')
      .forEach(el => el.remove());
    return copy.textContent.trim();
  };
  const labelFor = el => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return labelText(l);
    }
    const p = el.closest('label');
    return p ? labelText(p) : '';
  };
  const cssPath = el => {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 4) {
      let s = el.tagName.toLowerCase();
      const sib = el.parentElement
        ? [...el.parentElement.children].filter(c => c.tagName === el.tagName) : [];
      if (sib.length > 1) s += `:nth-of-type(${sib.indexOf(el) + 1})`;
      parts.unshift(s);
      el = el.parentElement;
    }
    return parts.join(' > ');
  };
  const sensitive = el => el.matches(
    'input[type=password], [data-sensitive], [data-apprentice-sensitive], ' +
    'input[autocomplete~=current-password], input[autocomplete~=new-password], ' +
    'input[autocomplete~=one-time-code], input[autocomplete~=cc-number], input[autocomplete~=cc-csc]'
  ) || !!el.closest('[data-sensitive], [data-apprentice-sensitive]');
  const describe = el => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(), type: el.type || null,
      text: sensitive(el) ? '' : (el.innerText || '').trim().slice(0, 80),
      aria_label: el.getAttribute('aria-label'), role: el.getAttribute('role'),
      id: el.id || null, name: el.getAttribute('name'),
      placeholder: el.getAttribute('placeholder'), label: labelFor(el),
      sensitive: sensitive(el),
      options: el.tagName === 'SELECT' && !sensitive(el)
        ? [...el.options].map(o => o.text) : null,
      css: cssPath(el),
      box: {x: r.x, y: r.y, w: r.width, h: r.height},
      viewport: {w: innerWidth, h: innerHeight},
    };
  };
  const visible = el => {
    const r = el.getBoundingClientRect(), style = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden'
      && style.display !== 'none';
  };
  window.__apprenticeState = () => ({
    url: location.href,
    elements: [...document.querySelectorAll('button, a, input, select, textarea, [role=button]')]
      .filter(visible).slice(0, 250).map(describe),
    messages: [...document.querySelectorAll('[role=status], [role=alert], output, p')]
      .filter(el => visible(el) && !sensitive(el)
        && !el.querySelector('input, select, textarea, button, a, [data-sensitive], [data-apprentice-sensitive]'))
      .map(el => ({css: cssPath(el), text: el.innerText.trim().slice(0, 300)}))
      .filter(m => m.text).slice(0, 100),
  });
  const emit = (kind, el, extra) =>
    window.__apprenticeEmit(JSON.stringify(
      {kind, t: Date.now(), url: location.href, element: describe(el), ...extra}));

  document.addEventListener('click', e => {
    if (!e.isTrusted) return;               // ignore synthetic clicks made by the page
    const el = e.target.closest('button, a, [role=button], input, select, textarea, label, summary') || e.target;
    if (el.tagName === 'SELECT') return;    // captured by 'change'
    if (el.tagName === 'TEXTAREA') return;
    if (el.tagName === 'INPUT' &&
        !['button','submit','checkbox','radio','file'].includes(el.type)) return;  // captured by 'change'
    if (['checkbox', 'radio'].includes(el.type)) emit('check', el,
      sensitive(el) ? {value: '<redacted>', sensitive: true} : {value: el.checked});
    else emit('click', el, {});
  }, true);

  document.addEventListener('change', e => {
    if (!e.isTrusted) return;
    const el = e.target;
    if (['checkbox', 'radio'].includes(el.type)) return; // captured by 'click'
    if (el.type === 'file') emit('upload', el, sensitive(el)
      ? {value: [...el.files].map(() => '<redacted>'), sensitive: true}
      : {value: [...el.files].map(f => f.name)});
    else if (sensitive(el)) emit(el.tagName === 'SELECT' ? 'select' : 'type', el,
                                {value: '<redacted>', sensitive: true});
    else if (el.tagName === 'SELECT') emit('select', el, {value: el.options[el.selectedIndex].text});
    else emit('type', el, {value: el.value});
  }, true);
})();
"""

def summarize(ev):
    e = ev.get("element", {})
    who = e.get("label") or e.get("text") or e.get("aria_label") or e.get("placeholder") or e.get("css", "")
    k = ev["kind"]
    if k == "navigate": return f"navigate -> {ev['url']}"
    if k == "download": return f"download  {ev['filename']}"
    if k == "click":    return f'click     "{who}"'
    return f'{k:<9} "{who}" = {ev.get("value")}'

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--name", required=True)
    args = ap.parse_args()

    if not args.name.strip() or args.name in (".", "..") or any(c in args.name for c in '<>:"/\\|?*'):
        ap.error("--name must be a single safe folder name, such as monthly_report")
    out = Path("recordings") / args.name
    out.mkdir(parents=True, exist_ok=True)
    steps, pending, n = [], [], 0

    def shot(page, label):
        nonlocal n
        fn = f"{n:03d}_{label}.png"; n += 1
        try:
            take_screenshot(page, out / fn)
            return fn
        except Exception:
            return None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 800})
        ctx.expose_binding("__apprenticeEmit", lambda src, payload: pending.append(json.loads(payload)))
        ctx.add_init_script(INJECT)
        page = ctx.new_page()
        page.on("download", lambda d: pending.append(
            {"kind": "download", "t": int(time.time() * 1000), "filename": d.suggested_filename, "url": page.url}))
        page.on("framenavigated", lambda f: f == page.main_frame and pending.append(
            {"kind": "navigate", "t": int(time.time() * 1000), "url": f.url}))

        page.goto(args.url, wait_until="load")
        prev = shot(page, "start")
        prev_state = capture_state(page)
        print("Recording. Do your task in the browser, then CLOSE THE BROWSER WINDOW to finish.")

        try:
            while not page.is_closed():
                page.wait_for_timeout(250)
                if not pending:
                    continue
                page.wait_for_timeout(400)          # let the UI settle
                batch = sorted(pending[:], key=lambda e: e["t"])
                pending.clear()
                after = shot(page, "after")
                state = capture_state(page)
                shared = sum(ev["kind"] == "click" for ev in batch) > 1
                for ev in batch:
                    ev.update(step=len(steps) + 1, before_shot=prev, after_shot=after,
                              before_state=prev_state, after_state=state, observations_shared=shared)
                    steps.append(ev)
                    print(f'{ev["step"]:>2}. {summarize(ev)}')
                prev = after
                prev_state = state
        except Exception:
            if not page.is_closed():
                raise
        # Keep actions received just before the window closed, even without an after image.
        for ev in sorted(pending, key=lambda event: event["t"]):
            ev.update(step=len(steps) + 1, before_shot=prev, after_shot=None,
                      before_state=prev_state, after_state=None)
            steps.append(ev)

        (out / "steps.json").write_text(json.dumps(
            {"name": args.name, "start_url": args.url,
             "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": steps}, indent=2), encoding="utf-8")
        print(f"\nSaved {len(steps)} steps to {out / 'steps.json'}")

if __name__ == "__main__":
    main()

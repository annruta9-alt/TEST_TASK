"""Browser automation scenario with visual effects (SVG cursor, ripple, callout)."""
import math
import os
import random
import re
import shutil
import time

from playwright.sync_api import sync_playwright

PATH_TO_VIDEO_FOLDER = "./video"

# DuckDuckGo blocks classic headless Chromium (HTTP 418 page), so by default the
# browser window is visible. Set QS_HEADLESS=1 to try new headless mode instead.
HEADLESS = os.environ.get("QS_HEADLESS") == "1"
MAX_ATTEMPTS = 3

VIEWPORT = {"width": 1280, "height": 720}

SEL_SEARCH_BOX = 'textarea[name="q"]'
SEL_SEARCH_BTN = 'form button[type="submit"]'
SEL_FIRST_RESULT = 'a[data-testid="result-title-a"]'

# Injected into every page (IIFE: add_init_script evaluates the string as-is).
OVERLAY_JS = r"""
(() => {
  if (window.__pwOverlay) return;
  window.__pwOverlay = true;
  const SIZE = 32, HALO = 64;
  let x = 20, y = 20;
  try {
    const s = JSON.parse(sessionStorage.getItem('__pw_cur') || 'null');
    if (s) { x = s.x; y = s.y; }
  } catch (e) {}

  const root = document.createElement('div');
  root.id = '__pw_overlay';
  root.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;' +
    'z-index:2147483647;pointer-events:none;';

  const halo = document.createElement('div');
  halo.style.cssText = `position:fixed;left:0;top:0;width:${HALO}px;height:${HALO}px;` +
    'border-radius:50%;background:rgba(255,220,0,0.35);pointer-events:none;' +
    'will-change:transform;';

  const cursor = document.createElement('div');
  cursor.style.cssText = `position:fixed;left:0;top:0;width:${SIZE}px;height:${SIZE}px;` +
    'pointer-events:none;will-change:transform;';
  cursor.innerHTML =
    `<svg xmlns="http://www.w3.org/2000/svg" width="${SIZE}" height="${SIZE}" viewBox="0 0 32 32">` +
    '<path d="M3 2 L3 26 L9.5 20 L14.5 30 L19 28 L14 18 L23 18 Z" ' +
    'fill="#fff" stroke="#000" stroke-width="2" stroke-linejoin="round"/></svg>';

  root.appendChild(halo);
  root.appendChild(cursor);

  function place() {
    halo.style.transform = `translate(${x - HALO / 2}px, ${y - HALO / 2}px)`;
    cursor.style.transform = `translate(${x}px, ${y}px)`;  // arrow tip == pointer
  }

  function mount() {
    const host = document.documentElement;
    if (host && !root.isConnected) host.appendChild(root);
  }
  mount();
  document.addEventListener('DOMContentLoaded', mount);
  new MutationObserver(mount).observe(document, { childList: true, subtree: false });
  place();

  window.addEventListener('mousemove', (e) => {
    x = e.clientX; y = e.clientY; place();
    try { sessionStorage.setItem('__pw_cur', JSON.stringify({ x, y })); } catch (e) {}
  }, true);

  window.addEventListener('mousedown', (e) => {
    const r = document.createElement('div');
    r.style.cssText = `position:fixed;left:${e.clientX - HALO / 2}px;top:${e.clientY - HALO / 2}px;` +
      `width:${HALO}px;height:${HALO}px;border-radius:50%;box-sizing:border-box;` +
      'border:3px solid rgba(255,140,0,0.9);background:rgba(255,140,0,0.15);' +
      'pointer-events:none;';
    root.appendChild(r);
    const a = r.animate(
      [{ transform: 'scale(0.1)', opacity: 1 }, { transform: 'scale(1)', opacity: 0 }],
      { duration: 600, easing: 'ease-out' });
    a.onfinish = () => r.remove();
  }, true);

  // Smooth glide driven by requestAnimationFrame (frame-locked, no IPC jitter).
  window.__pwGlide = (tx, ty, ms) => new Promise((resolve) => {
    const x0 = x, y0 = y, t0 = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - t0) / ms);
      const e = t * t * t * (t * (6 * t - 15) + 10);  // smootherstep
      x = x0 + (tx - x0) * e; y = y0 + (ty - y0) * e; place();
      if (t < 1) { requestAnimationFrame(step); }
      else {
        try { sessionStorage.setItem('__pw_cur', JSON.stringify({ x, y })); } catch (e) {}
        resolve(true);
      }
    };
    requestAnimationFrame(step);
  });

  window.__pwCallout = (text, bg, fg, fadeMs, pos) => {
    const old = document.getElementById('__pw_callout');
    if (old) old.remove();
    const c = document.createElement('div');
    c.id = '__pw_callout';
    c.textContent = text;
    c.style.cssText = `position:fixed;left:${pos.x}px;top:${pos.y}px;` +
      `background:${bg};color:${fg};font:16pt Arial,sans-serif;` +
      'padding:6px 14px;border:1px solid rgba(0,0,0,0.4);border-radius:3px;' +
      'white-space:nowrap;width:max-content;pointer-events:none;opacity:0;' +
      `transition:opacity ${fadeMs}ms ease;box-shadow:0 2px 8px rgba(0,0,0,0.3);`;
    root.appendChild(c);
    void c.offsetWidth;
    c.style.opacity = '1';
  };
  window.__pwCalloutHide = () => {
    const c = document.getElementById('__pw_callout');
    if (c) { c.style.opacity = '0'; setTimeout(() => c.remove(), 3000); }
  };
})();
"""

_page = None          # page used by the ShowCallout helper
_mouse = [20.0, 20.0]  # last known pointer position


# --------------------------------------------------------------------------- helpers
def ShowCallout(text, bgColor="#ffeb3b", textColor="#000000", fade=2.0,
                hold=2.0, x=None, y=None):
    """Show a yellow callout box (16pt, auto-sized) with fade-in/out of `fade` seconds."""
    if _page is None:
        return
    pos = {"x": 340 if x is None else x, "y": 70 if y is None else y}
    try:
        _page.evaluate(
            "([t,b,f,ms,p]) => window.__pwCallout(t,b,f,ms,p)",
            [text, bgColor, textColor, int(fade * 1000), pos],
        )
        time.sleep(fade + hold)
        _page.evaluate("() => window.__pwCalloutHide()")
        time.sleep(fade)
    except Exception:
        pass  # page navigated away - callout is simply gone


def _move_mouse(page, x, y, duration=None):
    """Glide the pointer in a straight line with smooth ease-in-out.

    The animation runs inside the page on requestAnimationFrame, so it is locked
    to the browser's frames and the recorded video is smooth. The real mouse is
    then placed on the target (hover/click work as usual). No randomness.
    """
    x0, y0 = _mouse
    dist = math.hypot(x - x0, y - y0)
    if dist < 1:
        return
    if duration is None:
        duration = min(1.6, max(0.6, dist / 600))
    try:
        page.evaluate("([x, y, ms]) => window.__pwGlide(x, y, ms)",
                      [x, y, int(duration * 1000)])
    except Exception:
        # fallback: time-based glide with real mouse events
        t_start = time.perf_counter()
        while True:
            t = (time.perf_counter() - t_start) / duration
            if t >= 1:
                break
            e = t * t * t * (t * (6 * t - 15) + 10)
            page.mouse.move(x0 + (x - x0) * e, y0 + (y - y0) * e)
            time.sleep(0.004)
    page.mouse.move(x, y)
    _mouse[0], _mouse[1] = x, y


def _find_visible(page, selector, timeout_ms):
    """Poll until some element matching `selector` is visible; return its box.

    The page may contain several matches (e.g. hidden submit buttons), so
    `.first` is not enough - take the first one that is actually visible.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        loc = page.locator(selector)
        try:
            count = loc.count()
        except Exception:
            count = 0
        for i in range(count):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                el.scroll_into_view_if_needed(timeout=2000)
                box = el.bounding_box()
                if box and box["width"] > 1 and box["height"] > 1:
                    return box
            except Exception:
                continue
        time.sleep(0.2)
    raise RuntimeError(f"no visible element for {selector}")


def _target_point(page, selector, timeout_ms):
    box = _find_visible(page, selector, timeout_ms)
    return (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def _move_and_click(page, selector, timeout_ms=30000):
    x, y = _target_point(page, selector, timeout_ms)
    _move_mouse(page, x, y)
    time.sleep(0.25)
    page.mouse.click(x, y)


def _type_human(page, text):
    for ch in text:
        page.keyboard.type(ch)
        time.sleep(random.uniform(0.07, 0.18) + (0.15 if ch == " " else 0))


def _safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().strip(".")
    return name or "video"


# --------------------------------------------------------------------------- main
STEALTH_JS = """
(() => {
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
})();
"""


class BlockedError(Exception):
    pass


def _check_blocked(page):
    u = page.url
    if "home-error" in u or "/418" in u or "static-pages" in u:
        raise BlockedError(f"blocked by site: {u}")


def _wait_results(page, timeout_s=60):
    """Poll for the first result; fail fast if DuckDuckGo shows its block page."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _check_blocked(page)
        try:
            if page.locator(SEL_FIRST_RESULT).first.is_visible():
                return
        except Exception:
            pass  # navigation in progress
        time.sleep(0.5)
    raise RuntimeError("results did not appear in time")


def _scenario(page, url, city):
    # 1. open the page
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    _check_blocked(page)
    page.wait_for_selector(SEL_SEARCH_BOX, timeout=30000)
    # 2. show the cursor (move once so it is drawn at its start point)
    page.mouse.move(_mouse[0], _mouse[1])
    time.sleep(1.0)

    ShowCallout(f"Searching for Magento developers in {city}", fade=1.0, hold=1.5)

    # 3. glide to the search box and click it
    _move_and_click(page, SEL_SEARCH_BOX)
    time.sleep(0.4)
    # 4. type the query human-like
    _type_human(page, f"Magento developer in {city}")
    time.sleep(0.6)

    # 5. click the search button (full navigation follows)
    _move_and_click(page, SEL_SEARCH_BTN, timeout_ms=10000)

    # 6. wait for results (client-side rendered, may be slow)
    _wait_results(page)
    time.sleep(1.0)
    page.mouse.move(_mouse[0], _mouse[1])  # make sure overlay is in place
    ShowCallout("First result", fade=0.8, hold=1.0, x=420, y=140)

    # 7. glide to the first result and click it
    _move_and_click(page, SEL_FIRST_RESULT)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    time.sleep(3.0)


def _attempt(p, url, city, language, folder):
    """One full recording attempt. Returns (ok, video_path)."""
    global _page
    _mouse[0], _mouse[1] = 20.0, 20.0
    launch_kwargs = dict(
        headless=HEADLESS,
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    if HEADLESS:
        launch_kwargs["channel"] = "chromium"  # new headless, closer to real Chrome
    browser = p.chromium.launch(**launch_kwargs)
    context = browser.new_context(
        viewport=VIEWPORT,
        record_video_dir=folder,
        record_video_size=VIEWPORT,
        locale=language or "en",
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{browser.version} Safari/537.36"),
    )
    context.add_init_script(STEALTH_JS)
    context.add_init_script(OVERLAY_JS)
    page = context.new_page()
    _page = page
    ok, video_path = False, None
    try:
        _scenario(page, url, city)
        ok = True
    except Exception as exc:
        print(f"qscript error: {exc!r}")
    finally:
        _page = None
        try:
            video_path = page.video.path() if page.video else None
        except Exception:
            video_path = None
        context.close()  # finalizes the video file
        browser.close()
    return ok, video_path


def CaptureVideo_Magento(url, company_name, language, country,
                         postcode, city, street, house_no):
    """Record the scenario; return absolute path to the .webm or None on failure."""
    folder = os.path.abspath(PATH_TO_VIDEO_FOLDER)
    os.makedirs(folder, exist_ok=True)
    final_path = os.path.join(folder, _safe_name(company_name) + ".webm")

    with sync_playwright() as p:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            ok, video_path = _attempt(p, url, city, language, folder)
            if ok and video_path and os.path.exists(video_path):
                try:
                    if os.path.exists(final_path):
                        os.remove(final_path)
                    shutil.move(video_path, final_path)
                except Exception as exc:
                    print(f"rename error: {exc!r}")
                    return None
                return final_path
            # failed attempt: drop its video and retry with a fresh browser
            if video_path and os.path.exists(video_path):
                try:
                    os.remove(video_path)
                except OSError:
                    pass
            if attempt < MAX_ATTEMPTS:
                print(f"retry {attempt + 1}/{MAX_ATTEMPTS} in 8 s ...", flush=True)
                time.sleep(8)
    return None
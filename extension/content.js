// Insta এক্সটেনশন — ইউটিউব/ফেসবুক/ইনস্টাগ্রামের পেজে "Insta-তে ডাউনলোড" বাটন
// তৈরি করেছেন MH & MIK
(() => {
  if (window.__instaButton) return;
  window.__instaButton = true;

  const host = location.hostname;
  const isYT = /(^|\.)youtube\.com$/.test(host);
  const isFB = /(^|\.)facebook\.com$/.test(host);
  const isIG = /(^|\.)instagram\.com$/.test(host);

  // বর্তমান পেজটাই কি একটি ভিডিওর পেজ?
  function pageIsVideo() {
    const p = location.pathname, q = location.search;
    if (isYT) return (p === "/watch" && /[?&]v=[\w-]/.test(q)) || /^\/(shorts|live)\/[\w-]+/.test(p);
    if (isFB) return (/^\/watch\/?$/.test(p) && /[?&]v=\d/.test(q)) || /^\/reel\/\d+/.test(p) ||
      /\/videos\/(?:[^/]+\/)?\d+/.test(p) || /^\/share\/(v|r)\/[\w-]+/.test(p) || p === "/video.php";
    if (isIG) return /^\/(?:[^/]+\/)?(reel|reels|p|tv)\/[\w-]+/.test(p);
    return false;
  }

  // ফিডে ভিডিওর পাশের লিংক খোঁজার প্যাটার্ন
  const LINK_SEL = isFB ? 'a[href*="/reel/"], a[href*="/videos/"], a[href*="/watch/?v="], a[href*="/share/v/"], a[href*="/share/r/"]'
    : isIG ? 'a[href*="/reel/"], a[href*="/p/"], a[href*="/tv/"]' : "";

  function visibleVideo() {
    let best = null, bestArea = 0;
    for (const v of document.querySelectorAll("video")) {
      const r = v.getBoundingClientRect();
      const w = Math.min(r.right, innerWidth) - Math.max(r.left, 0);
      const h = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0);
      if (w < 150 || h < 150) continue;
      if (w * h > bestArea) { best = v; bestArea = w * h; }
    }
    return best;
  }

  // ফিডের যে ভিডিও এখন সবচেয়ে বেশি দেখা যাচ্ছে, তার নিজস্ব লিংক
  function feedVideoLink() {
    if (!LINK_SEL) return null;
    let n = visibleVideo();
    for (let i = 0; n && i < 18; i++, n = n.parentElement) {
      const a = n.querySelector && n.querySelector(LINK_SEL);
      if (a && a.href) return a.href;
    }
    return null;
  }

  function targetUrl() {
    if (pageIsVideo()) return location.href;
    return feedVideoLink();
  }

  // ---- বাটন (Shadow DOM, যাতে সাইটের CSS না ভাঙে) ----
  const box = document.createElement("div");
  box.style.cssText = "all:initial;position:fixed;right:18px;bottom:22px;z-index:2147483647;display:none";
  const root = box.attachShadow({ mode: "open" });
  root.innerHTML = `
    <style>
      .wrap{display:flex;align-items:center;gap:6px;font-family:"Nirmala UI","Hind Siliguri",system-ui,sans-serif}
      button{all:unset;cursor:pointer;display:flex;align-items:center;gap:8px;background:#0B5D45;color:#fff;
        padding:11px 16px 11px 12px;border-radius:999px;font-size:15px;font-weight:600;line-height:1;
        box-shadow:0 6px 20px rgba(0,0,0,.35);transition:transform .12s,background .2s}
      button:hover{transform:translateY(-2px);background:#0d6e52}
      button:focus-visible{outline:3px solid #fff;outline-offset:2px}
      button.ok{background:#1f8f4e} button.err{background:#D9363E} button[disabled]{opacity:.85;cursor:wait}
      .x{padding:6px 9px;background:rgba(20,38,29,.82);font-size:14px;box-shadow:none}
      .q{padding:11px 10px;border-radius:999px;font-size:13px}
      .menu{position:absolute;right:0;bottom:calc(100% + 8px);min-width:190px;background:#17261E;border:1px solid #2A4034;
        border-radius:14px;padding:6px;box-shadow:0 10px 28px rgba(0,0,0,.45);display:none}
      .menu.open{display:block}
      .menu button{display:block;width:100%;box-sizing:border-box;background:transparent;color:#E6F0E9;border-radius:9px;
        padding:9px 12px;font-size:14px;font-weight:500;box-shadow:none;text-align:left;white-space:nowrap}
      .menu button:hover,.menu button:focus-visible{background:#1F3A2D;transform:none;outline:none}
      .menu .sep{height:1px;background:#2A4034;margin:4px 6px}
      .wrap{position:relative}
      svg{width:20px;height:20px;flex:none}
    </style>
    <div class="wrap">
      <button class="go" type="button" aria-label="Insta-তে ডাউনলোড করুন">
        <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 3v12M6.5 10.5 12 16l5.5-5.5M5 20h14"/></svg>
        <span class="t">Insta-তে ডাউনলোড</span>
      </button>
      <button class="q" type="button" aria-label="কোয়ালিটি বেছে নিন" aria-haspopup="true" title="কোয়ালিটি বেছে নিন">▾</button>
      <button class="x" type="button" aria-label="বাটন লুকান" title="এই পেজে লুকান">✕</button>
      <div class="menu" role="menu">
        <button type="button" role="menuitem" data-q="best">সেরা কোয়ালিটি</button>
        <button type="button" role="menuitem" data-q="1080">১০৮০p</button>
        <button type="button" role="menuitem" data-q="720">৭২০p</button>
        <button type="button" role="menuitem" data-q="480">৪৮০p</button>
        <div class="sep"></div>
        <button type="button" role="menuitem" data-q="audio">শুধু অডিও (MP3)</button>
      </div>
    </div>`;
  const go = root.querySelector(".go"), label = root.querySelector(".t"), hide = root.querySelector(".x");
  let hidden = false, busy = false, resetTimer = 0;

  function flash(text, cls, ms) {
    label.textContent = text; go.className = "go " + (cls || "");
    clearTimeout(resetTimer);
    resetTimer = setTimeout(() => { label.textContent = "Insta-তে ডাউনলোড"; go.className = "go"; }, ms);
  }

  const menu = root.querySelector(".menu"), qbtn = root.querySelector(".q");
  const closeMenu = () => menu.classList.remove("open");
  qbtn.addEventListener("click", e => { e.stopPropagation(); menu.classList.toggle("open"); });
  menu.addEventListener("click", e => {
    const b = e.target.closest("button[data-q]");
    if (b) { closeMenu(); sendNow(b.dataset.q); }
  });
  document.addEventListener("click", e => { if (!e.composedPath().includes(box)) closeMenu(); }, true);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeMenu(); }, true);

  go.addEventListener("click", () => sendNow(undefined));

  function sendNow(quality) {
    if (busy) return;
    const url = targetUrl() || location.href;
    busy = true; go.disabled = true; label.textContent = "পাঠানো হচ্ছে…";
    let done = false;
    const finish = r => {
      if (done) return; done = true; busy = false; go.disabled = false;
      if (r && r.ok) flash(r.duplicate ? "✓ ডাউনলোড চলছে" : "✓ পাঠানো হয়েছে, ডাউনলোড শুরু", "ok", 3500);
      else flash((r && r.error) || "পাঠানো যায়নি।", "err", 5500);
    };
    try {
      chrome.runtime.sendMessage({ type: "send", url, quality, title: document.title.replace(/^\(\d+\)\s*/, "") }, finish);
    } catch (e) {   // এক্সটেনশন রিলোড হলে পুরোনো পেজে এই এরর আসে
      finish({ ok: false, error: "পেজটি রিলোড (F5) করুন।" });
    }
    setTimeout(() => finish({ ok: false, error: "Insta সাড়া দিচ্ছে না।" }), 9000);
  }
  hide.addEventListener("click", () => { hidden = true; closeMenu(); box.style.display = "none"; });

  let lastHref = location.href;
  function refresh() {
    if (location.href !== lastHref) { lastHref = location.href; hidden = false; }
    const show = !hidden && !!targetUrl() && !document.fullscreenElement;
    box.style.display = show ? "block" : "none";
  }
  document.documentElement.appendChild(box);
  setInterval(refresh, 1000);
  refresh();
})();

// Insta এক্সটেনশন — ব্যাকগ্রাউন্ড (service worker)
// তৈরি করেছেন MH & MIK
const API = "http://127.0.0.1:8765";
const OFFLINE = "Insta অ্যাপ চালু নেই। আগে Insta.exe চালু করুন।";

async function call(path, options = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 6000);
  try {
    const r = await fetch(API + path, { ...options, signal: ctrl.signal });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: j.error || "অ্যাপ থেকে সমস্যা এসেছে।" };
    return j;
  } catch (e) {
    return { ok: false, offline: true, error: OFFLINE };
  } finally {
    clearTimeout(timer);
  }
}

async function ping() {
  // POST ব্যবহার হয় কারণ Chrome শুধু POST-এ Origin হেডার পাঠায়, আর অ্যাপ Origin দেখেই এক্সটেনশন চেনে
  const j = await call("/ext/ping", { method: "POST" });
  return j && j.ok ? { ok: true, version: j.version } : { ok: false, error: j.error || OFFLINE };
}

// লিংক পাঠায়; সেটিংস চালু থাকলে লগইন কুকিও (প্রাইভেট/লগইন-লাগা ভিডিওর জন্য)
const QUALITIES = ["best", "1080", "720", "480", "360", "audio"];

async function send(url, title, quality) {
  if (!/^https?:\/\//i.test(url || "")) return { ok: false, error: "এটি ভিডিওর লিংক নয়।" };
  const { sendCookies } = await chrome.storage.local.get({ sendCookies: false });
  let cookies = [];
  if (sendCookies) {
    try {
      cookies = (await chrome.cookies.getAll({ url })).map(c => ({
        name: c.name, value: c.value, domain: c.domain, path: c.path,
        secure: c.secure, httpOnly: c.httpOnly, expirationDate: c.expirationDate
      }));
    } catch (e) { /* এই সাইটের জন্য অনুমতি নেই, কুকি ছাড়াই চলবে */ }
  }
  return call("/ext/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, title: (title || "").slice(0, 150), cookies,
                           quality: QUALITIES.includes(quality) ? quality : undefined })
  });
}

function badge(ok) {
  chrome.action.setBadgeBackgroundColor({ color: ok ? "#0B5D45" : "#D9363E" });
  chrome.action.setBadgeText({ text: ok ? "✓" : "!" });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 3000);
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg && msg.type === "send") { send(msg.url, msg.title, msg.quality).then(reply); return true; }
  if (msg && msg.type === "ping") { ping().then(reply); return true; }
});

// ডান-ক্লিক মেনু: "Insta-তে ডাউনলোড করুন" → কোয়ালিটি বেছে নেওয়ার সাবমেনু
const MENU = [
  ["default", "সেটিংসের ডিফল্ট কোয়ালিটিতে"],
  ["best", "সেরা কোয়ালিটি"],
  ["1080", "১০৮০p"],
  ["720", "৭২০p"],
  ["480", "৪৮০p"],
  ["audio", "শুধু অডিও (MP3)"]
];
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    const contexts = ["link", "page", "video"];
    chrome.contextMenus.create({ id: "insta-send", title: "Insta-তে ডাউনলোড করুন", contexts });
    for (const [id, title] of MENU) {
      chrome.contextMenus.create({ id: "insta-q-" + id, parentId: "insta-send", title, contexts });
    }
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const id = String(info.menuItemId || "");
  if (!id.startsWith("insta-q-")) return;
  const q = id.slice("insta-q-".length);
  const url = info.linkUrl || info.pageUrl;
  const title = info.linkUrl ? "" : (tab && tab.title) || "";
  const r = await send(url, title, q === "default" ? undefined : q);
  badge(!!(r && r.ok));
});

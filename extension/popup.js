const $ = id => document.getElementById(id);

chrome.runtime.sendMessage({ type: "ping" }, r => {
  const on = r && r.ok;
  $("dot").className = "dot " + (on ? "on" : "off");
  $("st").textContent = on ? "Insta অ্যাপ চালু আছে" : "Insta অ্যাপ চালু নেই";
  $("send").disabled = !on; $("all").disabled = !on;
  if (!on) $("msg").textContent = "আগে Insta.exe চালু করুন।";
});

chrome.storage.local.get({ sendCookies: false }, v => { $("ck").checked = v.sendCookies; });
$("ck").addEventListener("change", () => chrome.storage.local.set({ sendCookies: $("ck").checked }));

const quality = () => $("q").value || undefined;

// এই URL-টি কি সমর্থিত সাইটের ভিডিও পেজ?
function isVideoUrl(u) {
  try {
    const x = new URL(u), h = x.hostname.replace(/^(www|m)\./, ""), p = x.pathname;
    if (/(^|\.)youtube\.com$/.test(h)) return (p === "/watch" && !!x.searchParams.get("v")) || /^\/(shorts|live)\/[\w-]+/.test(p);
    if (h === "youtu.be") return p.length > 1;
    if (/(^|\.)facebook\.com$/.test(h)) return /^\/(reel\/\d+|watch\/?$|share\/[vr]\/)/.test(p) || /\/videos\//.test(p) || p === "/video.php";
    if (h === "fb.watch") return true;
    if (/(^|\.)instagram\.com$/.test(h)) return /^\/(?:[^/]+\/)?(reel|reels|p|tv)\/[\w-]+/.test(p);
  } catch (e) { /* ভুল URL */ }
  return false;
}

$("all").addEventListener("click", async () => {
  const tabs = await chrome.tabs.query({ currentWindow: true });
  const seen = new Set(), list = [];
  for (const t of tabs) { if (t.url && isVideoUrl(t.url) && !seen.has(t.url)) { seen.add(t.url); list.push(t); } }
  const msg = $("msg"); msg.style.color = "";
  if (!list.length) { msg.textContent = "এই জানালায় কোনো ভিডিও ট্যাব নেই।"; return; }
  $("all").disabled = true; $("send").disabled = true; msg.textContent = "পাঠানো হচ্ছে…";
  let ok = 0, fail = 0;
  for (const t of list) {
    const r = await new Promise(res => chrome.runtime.sendMessage({ type: "send", url: t.url, title: t.title || "", quality: quality() }, res));
    if (r && r.ok) ok++; else fail++;
  }
  $("all").disabled = false; $("send").disabled = false;
  msg.textContent = "✓ " + ok + "টি ভিডিও পাঠানো হয়েছে।" + (fail ? " " + fail + "টি ব্যর্থ।" : "");
  msg.style.color = fail ? "#D9363E" : "#1f8f4e";
});

$("send").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  $("send").disabled = true; $("msg").textContent = "পাঠানো হচ্ছে…";
  chrome.runtime.sendMessage({ type: "send", url: tab && tab.url, title: (tab && tab.title) || "", quality: quality() }, r => {
    $("send").disabled = false;
    $("msg").textContent = r && r.ok ? "✓ পাঠানো হয়েছে, ডাউনলোড শুরু।" : ((r && r.error) || "পাঠানো যায়নি।");
    $("msg").style.color = r && r.ok ? "#1f8f4e" : "#D9363E";
  });
});

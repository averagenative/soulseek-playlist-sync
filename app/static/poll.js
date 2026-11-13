// Live regions: any element with data-live="<url>" is refreshed from that URL.
// Every 3 s while some region reports work in progress (a child with data-active="1"),
// every 15 s otherwise. Pauses while the tab is hidden. A region is redrawn only when
// its HTML changed. When it is, ticked checkboxes stay ticked, and an element marked
// data-keep="<name>" keeps whether it's open and how far it's scrolled. Buttons outside
// a region marked data-busy-lock are disabled while the region's data-busy="1", so they
// come back when a job finishes.
(function () {
  const regions = () => Array.from(document.querySelectorAll("[data-live]"));
  if (!regions().length) return;
  const busy = () => regions().some(r => r.querySelector('[data-active="1"]'));
  const last = new WeakMap();
  function swap(el, html) {
    const kept = {};
    el.querySelectorAll("[data-keep]").forEach(k => { kept[k.dataset.keep] = {open: k.open, top: k.scrollTop}; });
    const ticked = new Set(Array.from(el.querySelectorAll("input[type=checkbox][name]:checked"), b => b.name + "=" + b.value));
    el.innerHTML = html;
    el.querySelectorAll("input[type=checkbox][name]").forEach(b => { if (ticked.has(b.name + "=" + b.value)) b.checked = true; });
    el.querySelectorAll("[data-keep]").forEach(k => {
      const s = kept[k.dataset.keep];
      if (!s) return;
      if (k.tagName === "DETAILS") k.open = s.open;
      if (s.top) k.scrollTop = s.top;
    });
    const b = el.querySelector("[data-busy]");
    if (b) document.querySelectorAll("[data-busy-lock]").forEach(x => { x.disabled = b.dataset.busy === "1"; });
  }
  async function refresh() {
    await Promise.all(regions().map(async (el) => {
      if (el.contains(document.activeElement) && document.activeElement.matches("input, select, textarea")) return;
      try {
        const r = await fetch(el.dataset.live, {cache: "no-store", headers: {"X-Live": "1"}});
        // Signed out of the profile (a password changed elsewhere): the whole page goes to the sign-in page.
        if (r.status === 401 && r.headers.get("X-Signin")) { location.href = r.headers.get("X-Signin"); return; }
        if (!r.ok) return;
        const html = await r.text();
        if (last.get(el) === html) return;
        last.set(el, html);
        swap(el, html);
        document.dispatchEvent(new Event("live:updated"));
      } catch (e) {}
    }));
  }
  async function loop() {
    if (!document.hidden) await refresh();
    setTimeout(loop, busy() ? 3000 : 15000);
  }
  setTimeout(loop, busy() ? 3000 : 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();

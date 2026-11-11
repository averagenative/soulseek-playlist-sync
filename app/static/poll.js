// Live regions: any element with data-live="<url>" is refreshed from that URL.
// Every 3 s while some region reports work in progress (a child with data-active="1"),
// every 15 s otherwise. Pauses while the tab is hidden.
(function () {
  const regions = () => Array.from(document.querySelectorAll("[data-live]"));
  if (!regions().length) return;
  const busy = () => regions().some(r => r.querySelector('[data-active="1"]'));
  async function refresh() {
    await Promise.all(regions().map(async (el) => {
      if (el.contains(document.activeElement) && document.activeElement.matches("input, select, textarea")) return;
      try {
        const r = await fetch(el.dataset.live, {cache: "no-store", headers: {"X-Live": "1"}});
        if (r.ok) { el.innerHTML = await r.text(); document.dispatchEvent(new Event("live:updated")); }
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

// Refresh a live region while it says work is in progress: <div id="live" data-url="..."> with a
// child carrying data-active="1". Polls every 3 s while active, every 15 s otherwise.
(function () {
  const box = document.getElementById("live");
  if (!box) return;
  async function poll() {
    const active = box.querySelector("[data-active]")?.dataset.active === "1";
    try {
      const r = await fetch(box.dataset.url, {cache: "no-store"});
      if (r.ok) box.innerHTML = await r.text();
    } catch (e) {}
    setTimeout(poll, active ? 3000 : 15000);
  }
  setTimeout(poll, 3000);
})();

// Download ZIP: check the size first, asking before a big one and refusing one that's too big.
// While the ZIP is built and sent, a status line under the buttons shows how far along it is.
(function () {
  const box = document.getElementById("zip-status");

  function show(text, kind, percent) {
    if (!box) return;
    box.hidden = false;
    box.className = "notice zip-status" + (kind ? " " + kind : "");
    box.firstElementChild.textContent = text;
    box.querySelector(".meter span").style.width = (percent || 0) + "%";
  }

  async function follow(token) {
    let waiting = 0;
    for (;;) {
      await new Promise(r => setTimeout(r, 1000));
      let p;
      try {
        p = await (await fetch("/zip/progress?t=" + token, {cache: "no-store"})).json();
      } catch (e) { continue; }
      if (p.state === "done") return show(p.text, "ok", 100);
      if (p.state === "stopped") return show(p.text, "bad", 0);
      if (p.state === "waiting" && ++waiting > 120) {
        return show("The ZIP didn't start. Check your browser's downloads, then try again.", "bad", 0);
      }
      show(p.text, "", p.percent);
    }
  }

  document.addEventListener("click", async (e) => {
    const a = e.target.closest("a[data-zip-check]");
    if (!a || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
    e.preventDefault();
    if (a.dataset.checking) return;
    a.dataset.checking = "1";
    const label = a.textContent;
    a.textContent = "Checking size...";
    try {
      let c = null;
      try {
        c = await (await fetch(a.dataset.zipCheck, {cache: "no-store"})).json();
      } catch (err) {}
      if (c && !c.ok) { show(c.error, "bad", 0); return; }
      if (c && c.ask && !confirm(c.ask)) return;
      const token = Math.random().toString(36).slice(2, 12).padEnd(10, "0");
      show("Preparing the ZIP...", "", 0);
      location.href = a.href + "?t=" + token;
      follow(token);
    } finally {
      a.textContent = label;
      delete a.dataset.checking;
    }
  });
})();

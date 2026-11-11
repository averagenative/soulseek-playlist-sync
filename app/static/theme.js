// Theme menu: saves the choice in this browser and applies it right away.
(function () {
  const pick = document.getElementById("theme-pick");
  const root = document.documentElement;
  function syncBarColor() {
    const bg = getComputedStyle(root).getPropertyValue("--bg").trim();
    document.querySelectorAll('meta[name="theme-color"]').forEach(m => {
      if (root.dataset.theme) m.setAttribute("content", bg);
    });
  }
  let saved = "auto";
  try { saved = localStorage.getItem("theme") || "auto"; } catch (e) {}
  if (pick) {
    pick.value = saved;
    pick.addEventListener("change", () => {
      const t = pick.value;
      if (t === "auto") delete root.dataset.theme; else root.dataset.theme = t;
      try { localStorage.setItem("theme", t); } catch (e) {}
      syncBarColor();
    });
  }
  syncBarColor();
})();

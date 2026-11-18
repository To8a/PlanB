"use strict";
(() => {
  const root = document.querySelector("[data-partner-apps]");
  if (!root) return;
  const form = root.querySelector("form");
  const list = root.querySelector("[data-partner-list]");
  const descriptions = {
    burger: [root.dataset.burgerLogo, "Descubrí hamburguesas en La Plata", "Creá un grupo y compartí tus puntuaciones.", "Ir a ChesseeFinder"],
    movies: [root.dataset.moviesLogo, "Encontrá la película para el plan", "Buscá por actor o descripción y descubrí dónde verla.", "Ir a Streamly"]
  };
  fetch(root.dataset.url, {method: "POST", credentials: "same-origin", body: new FormData(form),
    headers: {Accept: "application/json"}}).then(response => {
      if (!response.ok || response.redirected) throw new Error("unavailable");
      return response.json();
    }).then(data => {
      if (!Array.isArray(data.apps)) return;
      for (const app of data.apps) {
        const copy = descriptions[app.kind];
        if (!copy || typeof app.url !== "string") continue;
        let url;
        try { url = new URL(app.url); } catch { continue; }
        if (url.protocol !== "https:" || url.username || url.password) continue;
        const link = document.createElement("a");
        link.className = "pb-plan-partner"; link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer";
        const icon = document.createElement("img"); icon.className = "pb-plan-partner__logo"; icon.src = copy[0]; icon.alt = "";
        const body = document.createElement("span"); body.className = "pb-plan-partner__body";
        const title = document.createElement("strong"); title.textContent = copy[1];
        const detail = document.createElement("small"); detail.textContent = copy[2];
        const action = document.createElement("span"); action.textContent = copy[3] + " ↗";
        body.append(title, detail, action); link.append(icon, body); list.append(link);
      }
      root.hidden = !list.children.length;
    }).catch(() => {});
})();

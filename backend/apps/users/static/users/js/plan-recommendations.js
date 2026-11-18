"use strict";

(() => {
  // Una sola inicialización y una sola generación automática por plan/documento.
  if (window.PlanBRecommendations) { window.PlanBRecommendations.init(); return; }
  const requests = new Map();
  const generations = new Map();
  const states = new Set(["not_generated", "ready", "failed", "pending", "not_confirmed"]);
  const categories = [
    ["catering.fast_food", "Comida rápida", "fast-food"],
    ["catering.restaurant", "Restaurante", "restaurant"],
    ["catering.cafe", "Café", "cafe"],
    ["catering.bar", "Bar", "bar"],
    ["catering.pub", "Bar", "bar"],
    ["adult.nightclub", "Discoteca", "nightclub"],
    ["leisure.park", "Parque", "park"],
    ["leisure.picnic", "Área de picnic", "park"],
    ["commercial.supermarket", "Supermercado", "shop"],
    ["commercial.food_and_drink.bakery", "Panadería", "bakery"],
    ["commercial.shopping_mall", "Centro comercial", "shopping"],
    ["entertainment.cinema", "Cine", "cinema"],
  ];
  const categoryPaths = {
    restaurant: ["M4 3v6a3 3 0 0 0 6 0V3", "M7 3v18", "M17 21V3c-3 2-4 5-4 9h4"],
    "fast-food": ["M3 12c0-4 4-7 9-7s9 3 9 7H3Z", "M3 16h18", "M4 19h16", "M8 9h.01M12 8h.01M16 9h.01"],
    cafe: ["M4 7h13v7a5 5 0 0 1-5 5H9a5 5 0 0 1-5-5V7Z", "M17 9h1a3 3 0 0 1 0 6h-1", "M6 3v1M10 3v1M14 3v1", "M3 21h16"],
    bar: ["M4 4h16l-7 8v8h3M11 20h2v-8L4 4Z"],
    nightclub: ["M9 18V5l11-2v13", "M9 18a3 2 0 1 1-3-2c1.7 0 3 .9 3 2ZM20 16a3 2 0 1 1-3-2c1.7 0 3 .9 3 2Z"],
    park: ["M12 21v-7", "M8 18H5l2-4H5l3-5H6l6-7 6 7h-2l3 5h-2l2 4h-3"],
    shop: ["M3 4h2l2 11h10l3-8H6", "M9 20h.01M17 20h.01", "M9 20a1 1 0 1 0 0 .01M17 20a1 1 0 1 0 0 .01"],
    bakery: ["M5 20h14l1-8a5 5 0 0 0-3-4 5 5 0 0 0-10 0 5 5 0 0 0-3 4l1 8Z", "M9 13v4M15 13v4"],
    shopping: ["M4 8h16l-1 13H5L4 8Z", "M9 9V6a3 3 0 0 1 6 0v3"],
    cinema: ["M4 5h16v14H4z", "M8 5v14M16 5v14M4 9h4M4 15h4M16 9h4M16 15h4", "m11 9 4 3-4 3V9Z"],
    place: ["M20 10c0 5-8 11-8 11S4 15 4 10a8 8 0 1 1 16 0Z", "M12 7a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z"],
  };
  const number = new Intl.NumberFormat("es-AR", {maximumFractionDigits: 1});
  const validCoordinates = item => typeof item.latitude === "number" && Number.isFinite(item.latitude)
    && Math.abs(item.latitude) <= 90 && typeof item.longitude === "number" && Number.isFinite(item.longitude)
    && Math.abs(item.longitude) <= 180;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function safeURL(value, google = false) {
    if (typeof value !== "string") return null;
    try {
      const url = new URL(value);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
      if (google && (url.protocol !== "https:" || url.hostname !== "www.google.com")) return null;
      return url.href;
    } catch { return null; }
  }

  function link(label, value, google = false) {
    const url = safeURL(value, google);
    if (!url) return null;
    const anchor = element("a", "pb-suggestion-link", label);
    anchor.href = url;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    anchor.setAttribute("aria-label", `${label} (se abre en otra pestaña)`);
    return anchor;
  }

  function category(item) {
    const values = Array.isArray(item.categories) ? item.categories : [];
    return categories.find(([key]) => values.some(value => typeof value === "string"
      && (value === key || value.startsWith(key + ".")))) || ["", "Lugar de interés", "place"];
  }

  function categoryIcon(kind) {
    const namespace = "http://www.w3.org/2000/svg";
    const icon = document.createElementNS(namespace, "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("class", "pb-suggestion-icon");
    icon.setAttribute("aria-hidden", "true");
    for (const drawing of categoryPaths[kind] || categoryPaths.place) {
      const path = document.createElementNS(namespace, "path");
      path.setAttribute("d", drawing);
      icon.append(path);
    }
    return icon;
  }

  function distance(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
    return value < 1000 ? `${Math.round(value)} m` : `${number.format(value / 1000)} km`;
  }

  async function request(url, method, form) {
    const endpoint = new URL(url, location.href);
    if (endpoint.origin !== location.origin) throw new Error("Endpoint inválido");
    const key = `${method}:${endpoint.href}`;
    if (requests.has(key)) return requests.get(key);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 90000);
    const promise = (async () => {
      try {
        const response = await fetch(endpoint.href, {
          method, credentials: "same-origin", cache: "no-store",
          headers: {Accept: "application/json"}, signal: controller.signal,
          ...(method === "POST" ? {body: new FormData(form)} : {}),
        });
        if (!response.ok || response.redirected) throw new Error("Servicio no disponible");
        const payload = await response.json();
        if (!payload || !states.has(payload.status) || !Array.isArray(payload.items)) throw new Error("Respuesta inválida");
        return payload;
      } finally {
        clearTimeout(timeout);
        requests.delete(key);
      }
    })();
    requests.set(key, promise);
    return promise;
  }

  function mount(root) {
    if (root.dataset.recommendationsInitialized) return;
    root.dataset.recommendationsInitialized = "true";
    const get = selector => root.querySelector(selector);
    const feedback = get("[data-suggestions-feedback]");
    const summary = get("[data-suggestions-summary]");
    const status = get("[data-suggestions-status]");
    const hint = get("[data-suggestions-hint]");
    const spinner = get("[data-suggestions-spinner]");
    const form = get("[data-suggestions-retry-form]");
    const retry = get("[data-suggestions-retry]");
    const refresh = get("[data-suggestions-refresh]");
    const results = get("[data-suggestions-results]");
    const list = get("[data-suggestions-list]");
    const mapElement = get("[data-suggestions-map]");
    const mapNote = get("[data-suggestions-map-note]");
    let busy = false;
    let map = null;
    let markers = [];
    let resizeObserver = null;
    let hasSnapshot = false;
    let refitWhenVisible = false;
    let visiblePoints = [];
    function resizeVisibleMap() {
      if (!map || !mapElement.clientWidth || !mapElement.clientHeight) return;
      map.invalidateSize({pan: false});
      if (refitWhenVisible) {
        map.fitBounds(visiblePoints, {padding: [32, 32], maxZoom: 16});
        refitWhenVisible = false;
      }
    }
    root.closest("[data-tool-panel]")?.addEventListener("planb:tool-open", () => requestAnimationFrame(resizeVisibleMap));

    function message(state, text, detail = "") {
      feedback.dataset.state = state;
      status.textContent = text;
      hint.textContent = detail;
      spinner.hidden = state !== "loading";
      retry.hidden = true;
      refresh.hidden = true;
    }

    function select(index, focus = false) {
      [...list.children].forEach((li, i) => {
        li.classList.toggle("is-selected", i === index);
        li.querySelector("[data-select-place]").setAttribute("aria-pressed", String(i === index));
        markers[i]?.getElement()?.classList.toggle("is-selected", i === index);
      });
      if (markers[index] && map) {
        map.panTo(markers[index].getLatLng(), {animate: false});
        markers[index].openPopup();
      }
      if (focus) {
        const button = list.children[index].querySelector("[data-select-place]");
        button.focus({preventScroll: true});
        button.scrollIntoView({block: "nearest", behavior: "instant"});
      }
    }

    function renderCard(item, index) {
      const li = element("li", "pb-suggestion");
      const article = element("article", "pb-suggestion-card");
      const [, label, kind] = category(item);
      const art = element("div", `pb-suggestion-art pb-suggestion-art--${kind}`);
      art.setAttribute("aria-hidden", "true");
      art.append(element("span", "pb-suggestion-rank", String(index + 1)), categoryIcon(kind));
      const body = element("div", "pb-suggestion-body");
      const meta = element("div", "pb-suggestion-meta");
      meta.append(element("span", "pb-suggestion-category", label));
      const meters = distance(item.distance_meters);
      if (meters) {
        const badge = element("span", "pb-suggestion-distance", meters);
        badge.title = "Distancia aproximada en línea recta";
        meta.append(badge);
      }
      const heading = element("h3");
      const name = typeof item.name === "string" && item.name.trim() ? item.name : "Lugar sugerido";
      const button = element("button", "pb-suggestion-select", name);
      button.type = "button";
      button.dataset.selectPlace = String(index);
      button.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-label", `${index + 1}. ${name}. Ubicar en el mapa`);
      heading.append(button);
      body.append(meta, heading, element("p", "pb-suggestion-address", item.address || "Dirección no disponible"));
      if (item.opening_hours) {
        const hours = element("p", "pb-suggestion-hours");
        hours.append(element("strong", "", "Horarios: "), document.createTextNode(String(item.opening_hours)));
        body.append(hours);
      }
      const actions = element("div", "pb-suggestion-actions");
      [link("Ver en Google", item.google_search_url, true), link("Abrir en Maps", item.google_maps_url, true),
        link("Sitio web", item.website)].filter(Boolean).forEach(anchor => actions.append(anchor));
      body.append(actions);
      article.append(art, body);
      li.append(article);
      return li;
    }

    function renderMap(items) {
      resizeObserver?.disconnect();
      resizeObserver = null;
      map?.remove();
      map = null;
      markers = [];
      mapElement.replaceChildren();
      mapNote.textContent = "";
      const valid = items.filter(validCoordinates);
      if (!valid.length || !window.L) {
        mapElement.hidden = true;
        mapNote.textContent = valid.length ? "El mapa no está disponible. Podés consultar todos los lugares en el listado."
          : "No hay coordenadas disponibles para mostrar en el mapa.";
        return;
      }
      mapElement.hidden = false;
      try {
        map = L.map(mapElement, {scrollWheelZoom: false});
        refitWhenVisible = !mapElement.clientWidth || !mapElement.clientHeight;
        visiblePoints = valid.map(item => [item.latitude, item.longitude]);
        // La página usa same-origin; los tiles necesitan Referer según política OSM.
        const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
          maxZoom: 19, referrerPolicy: "strict-origin-when-cross-origin",
          attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors',
        }).addTo(map);
        tiles.on("tileerror", () => {
          mapNote.textContent = "No pudimos cargar el fondo del mapa. Los lugares siguen disponibles en el listado.";
        });
        map.fitBounds(valid.map(item => [item.latitude, item.longitude]), {padding: [32, 32], maxZoom: 16});
        items.forEach((item, index) => {
          if (!validCoordinates(item)) return;
          const point = [item.latitude, item.longitude];
          const icon = element("span", "pb-suggestion-pin", String(index + 1));
          const popup = element("div", "pb-suggestion-popup");
          popup.append(element("strong", "", item.name || "Lugar sugerido"));
          if (item.address) popup.append(element("p", "", item.address));
          const marker = L.marker(point, {
            title: `${index + 1}. ${item.name || "Lugar sugerido"}`,
            icon: L.divIcon({className: "pb-suggestion-marker", html: icon, iconSize: [34, 42], iconAnchor: [17, 42]}),
          }).addTo(map).bindPopup(popup);
          marker.getElement().setAttribute("aria-label", `${index + 1}. ${item.name || "Lugar sugerido"}`);
          marker.on("click", () => select(index, true));
          markers[index] = marker;
        });
        if (typeof ResizeObserver !== "undefined") {
          resizeObserver = new ResizeObserver(resizeVisibleMap);
          resizeObserver.observe(mapElement);
        }
      } catch {
        map?.remove();
        map = null;
        markers = [];
        mapElement.hidden = true;
        mapNote.textContent = "El mapa no está disponible. Podés consultar todos los lugares en el listado.";
      }
    }

    function render(payload) {
      const items = payload.items.slice(0, 5).filter(item => item && typeof item === "object");
      hasSnapshot = Boolean(payload.has_snapshot || items.length || payload.status === "ready");
      summary.textContent = hasSnapshot && typeof payload.ai_summary === "string"
        && payload.ai_summary.length <= 320 ? payload.ai_summary.trim() : "";
      summary.hidden = !summary.textContent;
      retry.textContent = "Reintentar";
      results.hidden = !items.length;
      get("[data-suggestions-source]").hidden = !items.length;
      list.replaceChildren(...items.map(renderCard));
      renderMap(items);
      if (payload.status === "ready") {
        message("ready", items.length ? `${items.length} ${items.length === 1 ? "lugar para explorar" : "lugares para explorar"} con el grupo.`
          : "No encontramos lugares compatibles cerca del punto elegido.",
        items.length ? "Seleccioná un lugar para ubicarlo en el mapa." : "Podés seguir organizando el encuentro con la ubicación confirmada.");
        if (payload.is_stale) {
          message("ready", "Estas sugerencias corresponden a una versión anterior del plan.");
        }
        if (payload.can_refresh) {
          retry.textContent = "Actualizar sugerencias";
          retry.hidden = false;
        }
      } else if (payload.status === "failed") {
        message("failed", hasSnapshot ? "No pudimos actualizar las sugerencias. Estás viendo la última versión disponible."
          : "No pudimos generar sugerencias en este momento.",
          hasSnapshot ? "Podés seguir consultándolas y reintentar la actualización."
            : payload.error_code === "unsupported_intent" ? "Todavía no encontramos una categoría de lugares para esta actividad."
              : "El encuentro sigue confirmado. Podés volver a intentarlo cuando quieras.");
        retry.hidden = false;
        if (hasSnapshot) retry.textContent = "Reintentar actualización";
      } else if (payload.status === "pending") {
        message("pending", hasSnapshot ? "Estamos actualizando las sugerencias…" : "No terminamos de generar las sugerencias todavía.",
          hasSnapshot ? "Mientras tanto, podés consultar la última versión disponible."
            : "Otra consulta puede estar generándolas. Volvé a consultar el estado en unos momentos.");
        refresh.hidden = false;
        const updated = Date.parse(payload.updated_at);
        retry.hidden = !Number.isFinite(updated) || Date.now() - updated < 120000;
      } else if (payload.status === "not_confirmed") {
        message("not_confirmed", "Las sugerencias están disponibles sólo para encuentros confirmados y activos.");
      } else {
        message("not_generated", "Todavía no hay sugerencias para este encuentro.");
        retry.hidden = false;
      }
    }

    async function load(retrying = false) {
      if (busy) return;
      busy = true;
      retry.disabled = refresh.disabled = true;
      message("loading", hasSnapshot ? "Estamos actualizando las sugerencias…"
        : retrying ? "Estamos buscando opciones que encajen con este plan…" : "Consultando las sugerencias del plan…",
      hasSnapshot ? "Podés consultar la última versión disponible mientras tanto." : "");
      try {
        let payload = await request(retrying ? root.dataset.retryUrl : root.dataset.url, retrying ? "POST" : "GET", form);
        if ((payload.status === "not_generated" || payload.can_update === true) && !retrying) {
          if (payload.status !== "not_generated") render(payload);
          if (!generations.has(root.dataset.url)) {
            generations.set(root.dataset.url, request(root.dataset.url, "POST", form));
          }
          const generation = generations.get(root.dataset.url);
          if (generation) {
            message("loading", hasSnapshot ? "Estamos actualizando las sugerencias…"
              : "Estamos buscando opciones que encajen con este plan…",
            hasSnapshot ? "Podés consultar la última versión disponible mientras tanto." : "");
            try { payload = await generation; }
            catch (error) { generations.set(root.dataset.url, null); throw error; }
          }
        }
        render(payload);
      } catch {
        // No borrar un conjunto ya visible ni afectar al resto del detalle.
        message("unavailable", hasSnapshot ? "No pudimos actualizar las sugerencias. Estás viendo la última versión disponible."
          : "No pudimos consultar las sugerencias en este momento.",
          "El resto del plan sigue disponible. Consultá el estado o volvé a iniciar sesión si venció.");
        refresh.hidden = false;
        if (hasSnapshot) { retry.hidden = false; retry.textContent = "Reintentar actualización"; }
      } finally {
        busy = false;
        retry.disabled = refresh.disabled = false;
      }
    }
    list.addEventListener("click", event => {
      const button = event.target.closest("[data-select-place]");
      if (button) select(Number(button.dataset.selectPlace));
    });
    form.addEventListener("submit", event => { event.preventDefault(); load(true); });
    refresh.addEventListener("click", () => load());
    load();
  }

  function init() { document.querySelectorAll("[data-recommendations]").forEach(mount); }
  window.PlanBRecommendations = {init};
  init();
})();

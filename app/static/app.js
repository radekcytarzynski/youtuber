"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  loginView: $("login-view"),
  loginForm: $("login-form"),
  loginBtn: $("login-btn"),
  loginError: $("login-error"),
  password: $("password"),
  appView: $("app-view"),
  searchForm: $("search-form"),
  searchInput: $("search-input"),
  logoutBtn: $("logout-btn"),
  listTitle: $("list-title"),
  favBtn: $("fav-btn"),
  favorites: $("favorites"),
  favoritesList: $("favorites-list"),
  favoritesEmpty: $("favorites-empty"),
  status: $("status"),
  results: $("results"),
  moreBtn: $("more-btn"),
  player: $("player"),
  npThumb: $("np-thumb"),
  npTitle: $("np-title"),
  npChannel: $("np-channel"),
  audio: $("audio"),
  prevBtn: $("prev-btn"),
  nextBtn: $("next-btn"),
};

const state = {
  list: [],        // aktualnie wyświetlana lista
  queue: [],       // lista, z której gra odtwarzacz (nie zmienia się przy nawigacji)
  queueIndex: -1,
  channelId: null,   // identyfikator z adresu (#/channel/...), np. @nazwa
  channel: null,     // otwarty kanał z odpowiedzi serwera: { id, title }
  channelPage: 1,
  favorites: [],
  requestId: 0,    // ignorowanie spóźnionych odpowiedzi po zmianie widoku
};

class AuthError extends Error {}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { credentials: "same-origin", ...options, headers });
  let data = null;
  try {
    data = await res.json();
  } catch {
    // brak treści JSON
  }
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new AuthError("Wymagane logowanie.");
  }
  if (!res.ok) {
    const message = data && typeof data.detail === "string" ? data.detail : `Błąd serwera (${res.status}).`;
    throw new Error(message);
  }
  return data;
}

// --- widoki ---------------------------------------------------------------------

function showLogin() {
  els.appView.hidden = true;
  els.loginView.hidden = false;
  els.password.focus();
}

function showApp() {
  els.loginView.hidden = true;
  els.appView.hidden = false;
}

function setStatus(text, isError = false) {
  els.status.textContent = text;
  els.status.classList.toggle("error", isError);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function formatDuration(total) {
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

const viewsFormat = new Intl.NumberFormat("pl-PL", { notation: "compact", maximumFractionDigits: 1 });

function renderItem(item, index) {
  const li = el("li", "card");
  li.dataset.id = item.id;

  const play = el("button", "card-play");
  play.type = "button";
  play.title = "Odtwórz";
  play.addEventListener("click", () => playFromList(index));

  const thumb = el("div", "thumb");
  const img = document.createElement("img");
  img.loading = "lazy";
  img.alt = "";
  img.src = `/api/thumb/${item.id}`;
  img.addEventListener("error", () => img.remove(), { once: true });
  thumb.append(img);
  if (item.live) thumb.append(el("span", "badge live", "NA ŻYWO"));
  else if (item.duration) thumb.append(el("span", "badge", formatDuration(item.duration)));

  play.append(thumb, el("span", "card-title", item.title));
  li.append(play);

  const meta = el("div", "card-meta");
  if (item.channel && item.channel_id) {
    const link = el("a", "channel-link", item.channel);
    link.href = `#/channel/${encodeURIComponent(item.channel_id)}`;
    meta.append(link);
  } else if (item.channel) {
    meta.append(el("span", null, item.channel));
  }
  if (item.views != null) meta.append(el("span", "views", `${viewsFormat.format(item.views)} wyśw.`));
  li.append(meta);
  return li;
}

function setList(title, items) {
  state.list = items;
  els.listTitle.textContent = title;
  els.results.replaceChildren(...items.map(renderItem));
  markPlaying();
}

function markPlaying() {
  const current = state.queue[state.queueIndex];
  for (const li of els.results.children) {
    li.classList.toggle("playing", Boolean(current) && li.dataset.id === current.id);
  }
}

// --- ulubione kanały ------------------------------------------------------------

function isFavorite(channelId) {
  return state.favorites.some((fav) => fav.id === channelId);
}

function renderFavorites() {
  els.favoritesList.replaceChildren(
    ...state.favorites.map((fav) => {
      const li = el("li", "fav-item");
      const link = el("a", "fav-link", fav.title);
      link.href = `#/channel/${encodeURIComponent(fav.id)}`;
      const remove = el("button", "fav-remove", "×");
      remove.type = "button";
      remove.title = "Usuń z ulubionych";
      remove.setAttribute("aria-label", `Usuń ${fav.title} z ulubionych`);
      remove.addEventListener("click", () => {
        if (confirm(`Usunąć „${fav.title}” z ulubionych?`)) setFavorite(fav, false);
      });
      li.append(link, remove);
      return li;
    }),
  );
  els.favoritesEmpty.hidden = state.favorites.length > 0;
  updateFavButton();
}

function updateFavButton() {
  const channel = state.channel;
  els.favBtn.hidden = !channel;
  if (!channel) return;
  const active = isFavorite(channel.id);
  els.favBtn.textContent = active ? "★ W ulubionych" : "☆ Dodaj do ulubionych";
  els.favBtn.classList.toggle("active", active);
  els.favBtn.setAttribute("aria-pressed", String(active));
}

async function loadFavorites() {
  try {
    const data = await api("/api/favorites");
    state.favorites = data.items;
    renderFavorites();
  } catch (err) {
    if (!(err instanceof AuthError)) setStatus(`Nie udało się wczytać ulubionych: ${err.message}`, true);
  }
}

async function setFavorite(channel, add) {
  const path = `/api/favorites/${encodeURIComponent(channel.id)}`;
  try {
    const data = add
      ? await api(path, { method: "PUT", body: JSON.stringify({ title: channel.title }) })
      : await api(path, { method: "DELETE" });
    state.favorites = data.items;
    renderFavorites();
  } catch (err) {
    if (!(err instanceof AuthError)) setStatus(err.message, true);
  }
}

// --- nawigacja ------------------------------------------------------------------

function showFavoritesSection(visible) {
  els.favorites.hidden = !visible;
}

function route() {
  state.channel = null;
  updateFavButton();
  showFavoritesSection(false);

  const hash = location.hash.slice(1) || "/";
  const [path, query = ""] = hash.split("?");

  if (path === "/search") {
    const q = new URLSearchParams(query).get("q") || "";
    els.searchInput.value = q;
    if (q) return loadSearch(q);
  }
  const channelMatch = path.match(/^\/channel\/(.+)$/);
  if (channelMatch) return loadChannel(decodeURIComponent(channelMatch[1]), 1);

  state.requestId++;
  state.channelId = null;
  els.moreBtn.hidden = true;
  setList("", []);
  showFavoritesSection(true);
  setStatus("Wpisz frazę, @nazwę kanału albo wklej link do filmu lub kanału z YouTube.");
}

async function loadSearch(q) {
  const id = ++state.requestId;
  state.channelId = null;
  els.moreBtn.hidden = true;
  setList(`Wyniki: ${q}`, []);
  setStatus("Szukam…");
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
    if (id !== state.requestId) return;
    if (data.channel) {
      location.replace(`#/channel/${encodeURIComponent(data.channel)}`);
      return;
    }
    setList(`Wyniki: ${q}`, data.items);
    setStatus(data.items.length ? "" : "Brak wyników.");
  } catch (err) {
    if (id === state.requestId && !(err instanceof AuthError)) setStatus(err.message, true);
  }
}

async function loadChannel(channelId, page) {
  const id = ++state.requestId;
  els.moreBtn.hidden = true;
  if (page === 1) setList("Kanał", []);
  setStatus("Wczytuję…");
  try {
    const data = await api(`/api/channel/${encodeURIComponent(channelId)}?page=${page}`);
    if (id !== state.requestId) return;
    state.channelId = channelId;
    state.channelPage = page;
    state.channel = { id: data.channel.id, title: data.channel.title || channelId };
    updateFavButton();
    const items = page === 1 ? data.items : state.list.concat(data.items);
    setList(state.channel.title, items);
    els.moreBtn.hidden = !data.has_more;
    setStatus(items.length ? "" : "Ten kanał nie ma filmów.");
  } catch (err) {
    if (id === state.requestId && !(err instanceof AuthError)) setStatus(err.message, true);
  }
}

// --- odtwarzacz -----------------------------------------------------------------

function playFromList(index) {
  state.queue = state.list.slice();
  playQueue(index);
}

function playQueue(index) {
  const item = state.queue[index];
  if (!item) return;
  state.queueIndex = index;

  els.audio.src = `/api/audio/${item.id}`;
  els.audio.play().catch((err) => {
    if (err.name !== "AbortError") setStatus(`Nie udało się uruchomić odtwarzania: ${err.message}`, true);
  });

  els.player.hidden = false;
  els.npThumb.src = `/api/thumb/${item.id}`;
  els.npTitle.textContent = item.title;
  els.npChannel.textContent = item.channel || "";
  els.prevBtn.disabled = index <= 0;
  els.nextBtn.disabled = index >= state.queue.length - 1;
  document.title = `${item.title} · YT Audio`;

  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: item.title,
      artist: item.channel || "",
      artwork: [{ src: `/api/thumb/${item.id}`, sizes: "320x180", type: "image/jpeg" }],
    });
  }
  markPlaying();
}

async function explainAudioError() {
  const item = state.queue[state.queueIndex];
  if (!item) return;
  // Element <audio> nie pokazuje treści błędu, więc pytamy serwer o szczegóły.
  try {
    const res = await fetch(`/api/audio/${item.id}`, { headers: { Range: "bytes=0-0" }, credentials: "same-origin" });
    if (res.status === 401) return showLogin();
    if (!res.ok) {
      const data = await res.json().catch(() => null);
      return setStatus(data && data.detail ? data.detail : `Błąd odtwarzania (${res.status}).`, true);
    }
    res.body && res.body.cancel();
  } catch {
    // sieć niedostępna
  }
  setStatus("Nie udało się odtworzyć tego klipu. Spróbuj ponownie.", true);
}

els.audio.addEventListener("ended", () => playQueue(state.queueIndex + 1));
els.audio.addEventListener("error", () => {
  if (els.audio.getAttribute("src")) explainAudioError();
});
els.prevBtn.addEventListener("click", () => playQueue(state.queueIndex - 1));
els.nextBtn.addEventListener("click", () => playQueue(state.queueIndex + 1));
els.npThumb.addEventListener("error", () => els.npThumb.removeAttribute("src"));

if ("mediaSession" in navigator) {
  navigator.mediaSession.setActionHandler("previoustrack", () => playQueue(state.queueIndex - 1));
  navigator.mediaSession.setActionHandler("nexttrack", () => playQueue(state.queueIndex + 1));
}

// --- zdarzenia ------------------------------------------------------------------

els.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.loginError.textContent = "";
  els.loginBtn.disabled = true;
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ password: els.password.value }) });
    els.password.value = "";
    showApp();
    route();
    loadFavorites();
  } catch (err) {
    els.loginError.textContent = err.message;
    els.password.select();
  } finally {
    els.loginBtn.disabled = false;
  }
});

els.searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const q = els.searchInput.value.trim();
  if (!q) return;
  const target = `#/search?q=${encodeURIComponent(q)}`;
  if (location.hash === target) route();
  else location.hash = target;
});

els.favBtn.addEventListener("click", () => {
  if (state.channel) setFavorite(state.channel, !isFavorite(state.channel.id));
});

els.moreBtn.addEventListener("click", () => {
  if (state.channelId) loadChannel(state.channelId, state.channelPage + 1);
});

els.logoutBtn.addEventListener("click", async () => {
  els.audio.pause();
  els.audio.removeAttribute("src");
  els.audio.load();
  els.player.hidden = true;
  document.title = "YT Audio";
  try {
    await api("/api/logout", { method: "POST" });
  } catch {
    // i tak pokazujemy ekran logowania
  }
  showLogin();
});

window.addEventListener("hashchange", route);

(async function init() {
  try {
    await api("/api/me");
    showApp();
    route();
    loadFavorites();
  } catch (err) {
    if (!(err instanceof AuthError)) {
      showLogin();
      els.loginError.textContent = err.message;
    }
  }
})();

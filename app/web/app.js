// UI sin dependencias: carga el perfil editable, muestra la última búsqueda y pinta tarjetas.
const API = "/api/v1";

const criteriaList = document.getElementById("criteria");
const statusBox = document.getElementById("status");
const summaryBox = document.getElementById("summary");
const latestNote = document.getElementById("latest-note");
const resultsBox = document.getElementById("results");
const runForm = document.getElementById("run-form");
const runButton = document.getElementById("run-button");
const sortSelect = document.getElementById("sort-select");
const cardTemplate = document.getElementById("card-template");
const criteriaForm = document.getElementById("criteria-form");
const saveButton = document.getElementById("save-criteria");
const resetButton = document.getElementById("reset-criteria");
const profileSource = document.getElementById("profile-source");
const criteriaEditor = document.getElementById("criteria-editor");

// En escritorio el editor arranca abierto; en móvil, cerrado para ver los resultados.
if (criteriaEditor && window.matchMedia("(min-width: 821px)").matches) {
  criteriaEditor.open = true;
}

const FIELD_IDS = {
  location: "f-location",
  postalCode: "f-postal-code",
  radius: "f-radius",
  minPrice: "f-min-price",
  maxPrice: "f-max-price",
  minSize: "f-min-size",
  minRooms: "f-min-rooms",
  minYear: "f-min-year",
  energy: "f-energy",
  maxResults: "f-max-results",
  mustHave: "f-must-have",
  preferred: "f-preferred",
  preferences: "f-preferences",
};

const PORTAL_IDS = { idealista: "f-idealista", fotocasa: "f-fotocasa" };

const SOURCE_LABEL = {
  override: "Perfil guardado desde la UI",
  profile: "Perfil preconfigurado (SEARCH_PROFILE_JSON)",
  legacy: "Criterios preconfigurados (DAILY_*)",
};

const PORTAL_LABEL = { idealista: "Idealista", fotocasa: "Fotocasa" };

const AMENITY_LABEL = {
  swimming_pool: "Piscina",
  garden: "Jardín",
  terrace: "Terraza",
  air_conditioning: "Aire acondicionado",
  lift: "Ascensor",
  storage_room: "Trastero",
};

let currentListings = [];

function setStatus(message, isError = false) {
  statusBox.textContent = message;
  statusBox.classList.toggle("status--error", isError);
}

function euros(value) {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("es-ES", {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: 0,
  }).format(value);
}

function number(value, suffix = "") {
  if (value === null || value === undefined) return "—";
  return `${new Intl.NumberFormat("es-ES", { maximumFractionDigits: 1 }).format(value)}${suffix}`;
}

function renderCriteria(profile) {
  criteriaList.replaceChildren();
  const entries = [
    ["Zona", profile.postal_code ? `${profile.postal_code} · ${profile.radius_km} km` : profile.location],
    ["Portales", (profile.portals || []).join(", ") || "—"],
    ["Precio máx.", euros(profile.max_price_eur)],
    ["Superficie mín.", number(profile.min_size_m2, " m²")],
    ["Habitaciones mín.", profile.min_rooms ?? "—"],
    ["Año mín.", profile.min_year_built ?? "—"],
    ["Eficiencia mín.", profile.max_energy_rating ?? "—"],
    ["Obligatorio", (profile.must_have || []).join(", ") || "—"],
    ["Destacado", (profile.preferred_terms || []).join(", ") || "—"],
    ["Resultados", profile.max_results ?? "—"],
  ];
  for (const [label, value] of entries) {
    const item = document.createElement("li");
    item.textContent = `${label}: ${value}`;
    criteriaList.append(item);
  }
}

function fillForm(profile) {
  const value = (key) => document.getElementById(FIELD_IDS[key]);
  value("location").value = profile.location || "";
  value("postalCode").value = profile.postal_code || "";
  value("radius").value = profile.radius_km ?? "";
  value("minPrice").value = profile.min_price_eur ?? "";
  value("maxPrice").value = profile.max_price_eur ?? "";
  value("minSize").value = profile.min_size_m2 ?? "";
  value("minRooms").value = profile.min_rooms ?? "";
  value("minYear").value = profile.min_year_built ?? "";
  value("energy").value = profile.max_energy_rating || "";
  value("maxResults").value = profile.max_results ?? "";
  value("mustHave").value = (profile.must_have || []).join(", ");
  value("preferred").value = (profile.preferred_terms || []).join(", ");
  value("preferences").value = profile.preferences || "";

  const selected = new Set(profile.portals || []);
  for (const [portal, id] of Object.entries(PORTAL_IDS)) {
    document.getElementById(id).checked = selected.has(portal);
  }
}

function readNumber(key) {
  const raw = document.getElementById(FIELD_IDS[key]).value.trim();
  if (raw === "") return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

function readForm() {
  const portals = Object.entries(PORTAL_IDS)
    .filter(([, id]) => document.getElementById(id).checked)
    .map(([portal]) => portal);
  if (portals.length === 0) {
    throw new Error("Selecciona al menos un portal");
  }

  const location = document.getElementById(FIELD_IDS.location).value.trim();
  if (location.length < 2) {
    throw new Error("La zona es obligatoria (mínimo 2 caracteres)");
  }

  const postalCode = document.getElementById(FIELD_IDS.postalCode).value.trim();
  const mustHave = document
    .getElementById(FIELD_IDS.mustHave)
    .value.split(",")
    .map((term) => term.trim())
    .filter(Boolean);
  const preferences = document.getElementById(FIELD_IDS.preferences).value.trim();

  return {
    location,
    portals,
    postal_code: postalCode || null,
    radius_km: readNumber("radius"),
    min_price_eur: readNumber("minPrice"),
    max_price_eur: readNumber("maxPrice"),
    min_size_m2: readNumber("minSize"),
    min_rooms: readNumber("minRooms"),
    min_year_built: readNumber("minYear"),
    max_energy_rating: document.getElementById(FIELD_IDS.energy).value || null,
    max_results: readNumber("maxResults") ?? 40,
    must_have: mustHave,
    preferred_terms: document
      .getElementById(FIELD_IDS.preferred)
      .value.split(",")
      .map((term) => term.trim())
      .filter(Boolean),
    preferences: preferences || null,
  };
}

async function parseError(response) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    return `${response.status} ${response.statusText}`;
  }
  const code = body?.error?.code || response.status;
  const message = body?.error?.message || "Error desconocido";
  const fields = (body?.error?.details?.fields || [])
    .map((field) => `${(field.loc || []).slice(1).join(".") || "cuerpo"}: ${field.msg}`)
    .join("; ");
  return fields ? `${code}: ${message} (${fields})` : `${code}: ${message}`;
}

function sortListings(listingList, mode) {
  const sorted = [...listingList];
  const byPrice = (a, b) => a.price_eur - b.price_eur;
  const byMatches = (a, b) => (b.matched_terms?.length || 0) - (a.matched_terms?.length || 0);
  switch (mode) {
    case "price-asc":
      return sorted.sort(byPrice);
    case "price-desc":
      return sorted.sort((a, b) => b.price_eur - a.price_eur);
    case "size-desc":
      return sorted.sort((a, b) => (b.size_m2 ?? 0) - (a.size_m2 ?? 0));
    case "year-desc":
      return sorted.sort((a, b) => (b.year_built ?? 0) - (a.year_built ?? 0));
    default:
      return sorted.sort(
        (a, b) => byMatches(a, b) || b.relevance - a.relevance || byPrice(a, b),
      );
  }
}

function buildCard(listing) {
  const node = cardTemplate.content.firstElementChild.cloneNode(true);
  const media = node.querySelector(".card__media");
  const image = node.querySelector("img");
  const placeholder = node.querySelector(".card__placeholder");

  media.href = listing.url;
  node.querySelector(".card__portal").textContent =
    PORTAL_LABEL[listing.portal] || listing.portal;

  const photos = listing.photos || [];
  if (photos.length > 0) {
    image.src = photos[0];
    image.alt = listing.title;
    image.addEventListener("error", () => {
      image.remove();
      placeholder.hidden = false;
    });
  } else {
    image.remove();
    placeholder.hidden = false;
  }

  node.querySelector(".card__title").textContent = listing.title;
  node.querySelector(".card__location").textContent = listing.location || "Ubicación no disponible";

  const price = node.querySelector(".card__price");
  price.textContent = euros(listing.price_eur);
  if (listing.price_per_m2) {
    const perM2 = document.createElement("span");
    perM2.textContent = `  ·  ${euros(listing.price_per_m2)}/m²`;
    price.append(perM2);
  }

  const facts = node.querySelector(".card__facts");
  const factsData = [
    listing.size_m2 ? `${number(listing.size_m2, " m²")}` : null,
    listing.rooms !== null ? `${listing.rooms} hab.` : null,
    listing.bathrooms !== null ? `${listing.bathrooms} baño(s)` : null,
    listing.year_built ? `Año ${listing.year_built}` : null,
  ].filter(Boolean);
  for (const fact of factsData) {
    const item = document.createElement("li");
    item.textContent = fact;
    facts.append(item);
  }

  const amenities = node.querySelector(".card__amenities");
  for (const term of listing.matched_terms || []) {
    const item = document.createElement("li");
    item.className = "card__match";
    item.textContent = `★ ${term}`;
    amenities.prepend(item);
  }
  for (const amenity of listing.amenities || []) {
    const item = document.createElement("li");
    item.textContent = AMENITY_LABEL[amenity] || amenity;
    amenities.append(item);
  }

  node.querySelector(".card__reason").textContent = listing.reason || "";
  node.querySelector(".card__score").textContent = `Relevancia ${Math.round(
    listing.relevance * 100,
  )}%`;

  const link = node.querySelector(".card__link");
  link.href = listing.url;

  return node;
}

function renderListings() {
  resultsBox.replaceChildren();
  const sorted = sortListings(currentListings, sortSelect.value);
  for (const listing of sorted) {
    resultsBox.append(buildCard(listing));
  }
}

function renderSummary(result) {
  summaryBox.hidden = false;
  summaryBox.replaceChildren();
  const parts = [
    ["Encontrados", result.matched_count],
    ["Analizados", result.scraped_count],
    ["Descartados", result.discarded_count],
    ["Búsqueda", result.search_id],
  ];
  for (const [label, value] of parts) {
    const item = document.createElement("span");
    const strong = document.createElement("strong");
    strong.textContent = value;
    item.append(`${label}: `, strong);
    summaryBox.append(item);
  }
  for (const warning of result.warnings || []) {
    const item = document.createElement("span");
    item.className = "summary__warning";
    item.textContent = warning;
    summaryBox.append(item);
  }
}

function renderLatestNote(result) {
  const created = new Date(result.created_at);
  const when = Number.isNaN(created.getTime())
    ? result.run_date
    : created.toLocaleString("es-ES", { dateStyle: "short", timeStyle: "short" });
  latestNote.hidden = false;
  latestNote.textContent = `Resultados de la última búsqueda guardada (${when}).`;
}

async function loadProfile() {
  try {
    const response = await fetch(`${API}/search-profile`);
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body?.error?.message || "No se ha podido leer el perfil");
    }
    renderCriteria(body.profile);
    fillForm(body.profile);
    profileSource.textContent = SOURCE_LABEL[body.source] || "";
  } catch (error) {
    setStatus(
      `Perfil no disponible: ${error.message}. Revisa SEARCH_PROFILE_JSON en la configuración.`,
      true,
    );
  }
}

async function loadLatest() {
  try {
    const response = await fetch(`${API}/searches/latest`);
    if (response.status === 404) {
      return;
    }
    if (!response.ok) {
      throw new Error(await parseError(response));
    }
    const body = await response.json();
    currentListings = body.listings || [];
    renderSummary(body);
    renderListings();
    renderLatestNote(body);
  } catch (error) {
    setStatus(`No se han podido cargar los últimos resultados: ${error.message}`, true);
  }
}

async function saveCriteria(event) {
  event.preventDefault();
  saveButton.disabled = true;
  try {
    const payload = readForm();
    const response = await fetch(`${API}/search-profile`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      throw new Error(await parseError(response));
    }
    const body = await response.json();
    renderCriteria(body.profile);
    fillForm(body.profile);
    profileSource.textContent = SOURCE_LABEL[body.source] || "";
    setStatus("Criterios guardados. Pulsa «Ejecutar búsqueda» para aplicarlos ahora.");
  } catch (error) {
    setStatus(`No se han podido guardar los criterios: ${error.message}`, true);
  } finally {
    saveButton.disabled = false;
  }
}

async function resetCriteria() {
  resetButton.disabled = true;
  try {
    const response = await fetch(`${API}/search-profile`, { method: "DELETE" });
    if (!response.ok) {
      throw new Error(await parseError(response));
    }
    const body = await response.json();
    renderCriteria(body.profile);
    fillForm(body.profile);
    profileSource.textContent = SOURCE_LABEL[body.source] || "";
    setStatus("Criterios restaurados a los preconfigurados.");
  } catch (error) {
    setStatus(`No se han podido restaurar los criterios: ${error.message}`, true);
  } finally {
    resetButton.disabled = false;
  }
}

async function runSearch(event) {
  event.preventDefault();
  runButton.disabled = true;
  runButton.textContent = "Buscando…";
  setStatus("Ejecutando la búsqueda en los portales. Puede tardar un minuto…");
  summaryBox.hidden = true;
  resultsBox.replaceChildren();

  try {
    const response = await fetch(`${API}/search-profile/run`, { method: "POST" });
    const body = await response.json();
    if (!response.ok) {
      throw new Error(await parseError(response));
    }

    currentListings = body.listings || [];
    renderSummary(body);
    renderListings();
    renderLatestNote(body);
    setStatus(
      currentListings.length === 0
        ? "La búsqueda terminó sin anuncios que cumplan todos los requisitos."
        : "",
    );
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    runButton.disabled = false;
    runButton.textContent = "Ejecutar búsqueda";
  }
}

runForm.addEventListener("submit", runSearch);
criteriaForm.addEventListener("submit", saveCriteria);
resetButton.addEventListener("click", resetCriteria);
sortSelect.addEventListener("change", () => {
  if (currentListings.length > 0) renderListings();
});

loadProfile();
loadLatest();

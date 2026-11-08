// UI sin dependencias: carga el perfil configurado, lo ejecuta y pinta tarjetas.
const API = "/api/v1";

const criteriaList = document.getElementById("criteria");
const statusBox = document.getElementById("status");
const summaryBox = document.getElementById("summary");
const resultsBox = document.getElementById("results");
const runForm = document.getElementById("run-form");
const runButton = document.getElementById("run-button");
const sortSelect = document.getElementById("sort-select");
const cardTemplate = document.getElementById("card-template");

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
    ["Precio máx.", euros(profile.max_price_eur)],
    ["Superficie mín.", number(profile.min_size_m2, " m²")],
    ["Habitaciones mín.", profile.min_rooms ?? "—"],
    ["Año mín.", profile.min_year_built ?? "—"],
    ["Obligatorio", (profile.must_have || []).join(", ") || "—"],
    ["Resultados", profile.max_results ?? "—"],
  ];
  for (const [label, value] of entries) {
    const item = document.createElement("li");
    item.textContent = `${label}: ${value}`;
    criteriaList.append(item);
  }
}

function sortListings(listingList, mode) {
  const sorted = [...listingList];
  const byPrice = (a, b) => a.price_eur - b.price_eur;
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
      return sorted.sort((a, b) => b.relevance - a.relevance || byPrice(a, b));
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
}

async function loadProfile() {
  try {
    const response = await fetch(`${API}/search-profile`);
    const body = await response.json();
    if (!response.ok) {
      throw new Error(body?.error?.message || "No se ha podido leer el perfil");
    }
    renderCriteria(body.profile);
  } catch (error) {
    setStatus(
      `Perfil no disponible: ${error.message}. Revisa SEARCH_PROFILE_JSON en la configuración.`,
      true,
    );
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
      const code = body?.error?.code || response.status;
      const message = body?.error?.message || "Error desconocido";
      throw new Error(`${code}: ${message}`);
    }

    currentListings = body.listings || [];
    renderSummary(body);
    renderListings();
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
sortSelect.addEventListener("change", () => {
  if (currentListings.length > 0) renderListings();
});

loadProfile();

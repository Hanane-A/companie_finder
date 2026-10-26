function fold(value) {
  return (value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

function prospectTable() {
  const params = new URLSearchParams(window.location.search);
  return {
    q: params.get("q") || "",
    shown: 0,
    total: 0,
    init() {
      this.count();
      this.$watch("q", () => {
        this.syncUrl();
        this.count();
      });
    },
    match(blob) {
      const query = fold(this.q).trim();
      if (!query) return true;
      const haystack = fold(blob || "");
      return query.split(/\s+/).every((token) => haystack.includes(token));
    },
    count() {
      if (!this.$refs.rows) {
        this.total = 0;
        this.shown = 0;
        return;
      }
      const rows = [...this.$refs.rows.querySelectorAll("tr[data-blob]")];
      this.total = rows.length;
      this.shown = rows.filter((row) => this.match(row.dataset.blob)).length;
    },
    syncUrl() {
      const url = new URL(window.location.href);
      const value = this.q.trim();
      if (value) url.searchParams.set("q", value);
      else url.searchParams.delete("q");
      history.replaceState(null, "", url);
    },
    exportHref() {
      const url = new URL(window.location.href);
      url.pathname = "/export.csv";
      ["importes", "maj", "enregistre", "supprime", "erreur"].forEach((key) => {
        url.searchParams.delete(key);
      });
      const value = this.q.trim();
      if (value) url.searchParams.set("q", value);
      else url.searchParams.delete("q");
      return `${url.pathname}${url.search}`;
    },
  };
}

function pickProspects(mode) {
  document.querySelectorAll("[data-pick]").forEach((input) => {
    if (mode === "new") input.checked = input.dataset.known === "0";
    else if (mode === "none") input.checked = false;
    else input.checked = true;
  });
}

async function deskStatut(select, id) {
  const previous = select.dataset.status;
  select.dataset.status = select.value;
  select.disabled = true;
  try {
    const response = await fetch(`/prospects/${id}/statut`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ statut: select.value }),
    });
    if (!response.ok) throw new Error("statut");
  } catch (error) {
    select.value = previous;
    select.dataset.status = previous;
  } finally {
    select.disabled = false;
  }
}

document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-export]");
  if (link) {
    event.preventDefault();
    const url = new URL(window.location.href);
    url.pathname = "/export.csv";
    ["importes", "maj", "enregistre", "supprime", "erreur"].forEach((key) => {
      url.searchParams.delete(key);
    });
    const input = document.querySelector("[data-live-q]");
    if (input) {
      const value = input.value.trim();
      if (value) url.searchParams.set("q", value);
      else url.searchParams.delete("q");
    }
    window.location.assign(`${url.pathname}${url.search}`);
    return;
  }
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const value = button.getAttribute("data-copy") || "";
  navigator.clipboard.writeText(value).then(() => {
    const previous = button.textContent;
    button.textContent = "Copié";
    window.setTimeout(() => {
      button.textContent = previous;
    }, 1200);
  });
});

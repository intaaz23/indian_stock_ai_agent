(function () {
  const form = document.getElementById("screenerForm");
  const runBtn = document.getElementById("runScreenBtn");
  const table = document.getElementById("screenerTable");
  const tbody = document.getElementById("screenerBody");
  const quickSearch = document.getElementById("quickSearch");

  const pageSizeEl = document.getElementById("pageSize");
  const prevBtn = document.getElementById("prevPage");
  const nextBtn = document.getElementById("nextPage");
  const rowInfo = document.getElementById("rowInfo");

  let currentPage = 1;
  let pageSize = pageSizeEl ? parseInt(pageSizeEl.value, 10) : 25;

  // submit loading
  if (form && runBtn) {
    form.addEventListener("submit", function () {
      runBtn.disabled = true;
      runBtn.textContent = "Running...";
    });
  }

  // search filter
  function applySearchFilter() {
    if (!table || !quickSearch) return;
    const q = quickSearch.value.toLowerCase().trim();
    const rows = Array.from(tbody.querySelectorAll("tr"));
    rows.forEach((row) => {
      const cells = row.querySelectorAll("td");
      if (cells.length < 3) return;
      const symbol = (cells[1].textContent || "").toLowerCase();
      const company = (cells[2].textContent || "").toLowerCase();
      const match = symbol.includes(q) || company.includes(q);
      row.dataset.filtered = match ? "1" : "0";
    });
  }

  // pagination
  function renderPage() {
    if (!tbody) return;
    const rows = Array.from(tbody.querySelectorAll("tr")).filter(r => r.querySelectorAll("td").length > 1);
    const filtered = rows.filter(r => r.dataset.filtered !== "0");

    const total = filtered.length;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    if (currentPage > totalPages) currentPage = totalPages;

    const start = (currentPage - 1) * pageSize;
    const end = start + pageSize;

    rows.forEach(r => { r.style.display = "none"; });
    filtered.slice(start, end).forEach(r => { r.style.display = ""; });

    if (rowInfo) {
      const s = total === 0 ? 0 : start + 1;
      const e = Math.min(end, total);
      rowInfo.textContent = `Showing ${s}-${e} of ${total}`;
    }

    if (prevBtn) prevBtn.disabled = currentPage <= 1;
    if (nextBtn) nextBtn.disabled = currentPage >= totalPages;
  }

  if (quickSearch) {
    quickSearch.addEventListener("input", () => {
      currentPage = 1;
      applySearchFilter();
      renderPage();
    });
  }

  if (pageSizeEl) {
    pageSizeEl.addEventListener("change", () => {
      pageSize = parseInt(pageSizeEl.value, 10);
      currentPage = 1;
      renderPage();
    });
  }

  if (prevBtn) prevBtn.addEventListener("click", () => {
    currentPage = Math.max(1, currentPage - 1);
    renderPage();
  });

  if (nextBtn) nextBtn.addEventListener("click", () => {
    currentPage += 1;
    renderPage();
  });

  // sorting
  if (table) {
    const headers = table.querySelectorAll("thead th");
    let sortState = { index: -1, dir: "asc" };

    const parseNum = (txt) => {
      const cleaned = (txt || "").replace(/[^0-9.-]/g, "");
      const n = parseFloat(cleaned);
      return Number.isFinite(n) ? n : -Infinity;
    };

    headers.forEach((th, idx) => {
      th.style.cursor = "pointer";
      th.addEventListener("click", () => {
        const type = th.dataset.sort || "string";
        const rows = Array.from(tbody.querySelectorAll("tr")).filter(r => r.querySelectorAll("td").length > 1);
        const dir = (sortState.index === idx && sortState.dir === "asc") ? "desc" : "asc";
        sortState = { index: idx, dir };

        rows.sort((a, b) => {
          const av = (a.children[idx]?.innerText || "").trim();
          const bv = (b.children[idx]?.innerText || "").trim();

          if (type === "number") {
            const na = parseNum(av);
            const nb = parseNum(bv);
            return dir === "asc" ? na - nb : nb - na;
          }
          return dir === "asc" ? av.localeCompare(bv) : bv.localeCompare(av);
        });

        rows.forEach(r => tbody.appendChild(r));
        currentPage = 1;
        applySearchFilter();
        renderPage();
      });
    });

    // default sort by AI Score (col index 13) desc
    const aiScoreIndex = 13;
    const rows = Array.from(tbody.querySelectorAll("tr")).filter(r => r.querySelectorAll("td").length > 1);
    rows.sort((a, b) => {
      const av = parseNum(a.children[aiScoreIndex]?.innerText || "");
      const bv = parseNum(b.children[aiScoreIndex]?.innerText || "");
      return bv - av;
    });
    rows.forEach(r => tbody.appendChild(r));
  }

  // initial
  if (tbody) {
    Array.from(tbody.querySelectorAll("tr")).forEach(r => r.dataset.filtered = "1");
  }
  applySearchFilter();
  renderPage();
})();
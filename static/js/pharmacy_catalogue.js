// Medicine catalogue: auto-apply the filter selects, debounce the search box,
// and bring the most recently saved card into view.
(function () {
  "use strict";
  const searchForm = document.querySelector("form.ph-catalogue-search");
  if (searchForm) {
    let debounce = null;
    searchForm.addEventListener("change", (event) => {
      if (event.target.tagName === "SELECT") {
        searchForm.submit();
      }
    });
    const searchInput = searchForm.querySelector("input[name=q]");
    if (searchInput) {
      searchInput.addEventListener("input", () => {
        clearTimeout(debounce);
        debounce = setTimeout(() => searchForm.submit(), 450);
      });
    }
  }
  const savedCard = document.querySelector(".ph-med-card.is-saved");
  if (savedCard) {
    savedCard.scrollIntoView({ block: "center" });
  }

  // View toggle: grid <-> table
  const grid = document.querySelector(".ph-med-grid");
  const table = document.querySelector(".ph-med-table-wrap");
  const viewBtns = document.querySelectorAll(".ph-view-btn");
  if (grid && table && viewBtns.length) {
    const applyView = (mode) => {
      const isTable = mode === "table";
      grid.hidden = isTable;
      table.hidden = !isTable;
      viewBtns.forEach((btn) => {
        const active = btn.dataset.view === mode;
        btn.classList.toggle("is-active", active);
        btn.setAttribute("aria-pressed", active ? "true" : "false");
      });
    };
    const savedView = localStorage.getItem("ph-catalogue-view");
    if (savedView === "table") {
      applyView("table");
    }
    viewBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        const mode = btn.dataset.view === "table" ? "table" : "grid";
        applyView(mode);
        localStorage.setItem("ph-catalogue-view", mode);
      });
    });
  }
})();

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
})();

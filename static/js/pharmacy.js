document.querySelectorAll("[data-match]").forEach(function (select) {
  const row = select.closest("[data-rx]");
  const remaining = Number(row.dataset.remaining);
  function updateStock() {
    const value = select.selectedOptions[0]?.dataset.stock;
    row.querySelector("[data-available]").textContent = value ?? "—";
    row.querySelector("[data-quantity]").max = Math.min(9999, remaining, value ?? remaining);
    row.querySelector("[data-shortage]").textContent = value !== undefined && Number(value) < remaining
      ? "Only " + value + " units available for this balance." : "";
  }
  select.addEventListener("change", updateStock);
  updateStock();
});

const preview = document.getElementById("stockPreview");
if (preview) {
  const form = preview.closest("form");
  const packs = document.getElementById("packSizes");
  const data = JSON.parse((packs || document.getElementById("batchBalances")).textContent);
  function updatePreview() {
    if (packs) {
      const size = data[form.elements.medicine.value];
      const count = form.elements.packs.valueAsNumber;
      preview.textContent = size && Number.isInteger(count) && count > 0
        ? count + " packs × " + size + " units = " + (count * size) + " dispensing units"
        : "Choose a medicine and enter the number of packs.";
    } else {
      const batch = data[form.elements.batch.value];
      preview.textContent = batch
        ? "On hand " + batch.on_hand + " · Reserved " + batch.reserved + " · Available " + batch.available
        : "Choose a batch to view its balances.";
    }
  }
  form.addEventListener("input", updatePreview);
  form.addEventListener("change", updatePreview);
  updatePreview();
}

document.querySelectorAll("[data-import-row]").forEach(function (row) {
  const output = row.querySelector("[data-conversion]");
  function value(field) {
    return row.querySelector('[name$="-' + field + '"]')?.value || "";
  }
  function updateConversion() {
    const quantity = Number(value("quantity"));
    const pack = Number(value("pack_size"));
    const cost = value("purchase_cost");
    const price = value("selling_price");
    if (quantity > 0 && pack > 0 && value("quantity_basis") && value("price_basis") && cost !== "" && price !== "") {
      const units = quantity * (value("quantity_basis") === "packs" ? pack : 1);
      const divisor = value("price_basis") === "packs" ? pack : 1;
      output.textContent = units + " dispensing units · Purchase ₹" + (Number(cost) / divisor).toFixed(2)
        + " per unit · Selling ₹" + (Number(price) / divisor).toFixed(2) + " per unit";
    } else {
      output.textContent = "Enter quantity, pack size, prices and their units to preview this receipt.";
    }
  }
  row.addEventListener("input", updateConversion);
  row.addEventListener("change", updateConversion);
  updateConversion();
});

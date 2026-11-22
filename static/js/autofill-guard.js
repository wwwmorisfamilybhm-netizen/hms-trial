/* Strip browser-autofilled values from forms that opt out of autofill.
 *
 * Chrome ignores autocomplete="off" for its address/name autofill, silently
 * filling the operator's own saved identity (name, phone, email, address)
 * into fields that belong to somebody else — e.g. a patient being registered.
 * This guard clears values the browser filled without any keyboard/mouse
 * interaction from the operator, so real typed input is never removed.
 */
(function () {
  function isAutofilled(el) {
    try {
      return el.matches(":autofill") || el.matches(":-webkit-autofill");
    } catch (err) {
      return false;
    }
  }

  document.querySelectorAll('form[autocomplete="off"]').forEach(function (form) {
    var touched = new WeakSet();

    // Only genuine user interaction marks a field as touched. Browser autofill
    // fires no key, pointer, or paste events.
    ["keydown", "mousedown", "pointerdown", "paste", "drop"].forEach(function (type) {
      form.addEventListener(
        type,
        function (event) {
          if (event.target && event.target.matches("input, textarea, select")) {
            touched.add(event.target);
          }
        },
        true
      );
    });

    function clearAutofilled() {
      form.querySelectorAll("input, textarea").forEach(function (el) {
        if (touched.has(el) || !isAutofilled(el)) return;
        el.value = "";
        el.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }

    // Autofill lands after page load; re-check a few times while the page is
    // still fresh and the operator cannot have typed into every field yet.
    [0, 250, 600, 1200, 2000].forEach(function (delay) {
      window.setTimeout(clearAutofilled, delay);
    });
  });
})();

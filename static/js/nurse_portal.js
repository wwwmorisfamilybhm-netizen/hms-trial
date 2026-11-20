/* ============================================================
   Cams HMS — Nurse Portal
   Live patient picker + faster vitals entry for the triage desk.
   The triage page itself wires its own form via the inline
   bootstrap it renders (window.NURSE_TRIAGE).
   ============================================================ */
(function () {
  "use strict";
  function debounce(fn, wait) {
    var t = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, wait);
    };
  }

  function csrfToken() {
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    if (input) return input.value;
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  /* ── Patient picker (dashboard / queue) ───────────────────── */
  function initPickers() {
    document.querySelectorAll("[data-nurse-picker]").forEach(function (wrap) {
      var input = wrap.querySelector("input[type=search]");
      var results = wrap.querySelector(".np-picker-results");
      if (!input || !results) return;

      var search = debounce(function () {
        var q = input.value.trim();
        if (q.length < 2) { results.hidden = true; results.innerHTML = ""; return; }
        fetch("/api/nurse/patients/?q=" + encodeURIComponent(q), {
          headers: { "X-Requested-With": "XMLHttpRequest" },
          credentials: "same-origin",
        })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            results.innerHTML = "";
            if (!data.ok || !data.results.length) {
              results.innerHTML = '<div class="np-picker-empty">No matching patients.</div>';
              results.hidden = false;
              return;
            }
            data.results.forEach(function (p) {
              var a = document.createElement("a");
              a.href = p.url;
              a.innerHTML =
                '<span class="np-pk-avatar">' + escapeHtml(p.initials) + "</span>" +
                "<span><strong>" + escapeHtml(p.name) + "</strong>" +
                (p.emergency ? ' <span class="np-badge emergency">Emergency</span>' : "") +
                (p.in_queue ? ' <span class="np-badge teal">In queue</span>' : "") +
                '<div class="np-pk-meta">' + escapeHtml(p.meta) + "</div></span>";
              results.appendChild(a);
            });
            results.hidden = false;
          })
          .catch(function () {
            results.innerHTML = '<div class="np-picker-empty">Search unavailable — try again.</div>';
            results.hidden = false;
          });
      }, 220);

      input.addEventListener("input", search);
      input.addEventListener("focus", function () { if (input.value.trim().length >= 2) search(); });
      document.addEventListener("click", function (e) {
        if (!wrap.contains(e.target)) results.hidden = true;
      });
      input.addEventListener("keydown", function (e) {
        if (e.key === "Escape") results.hidden = true;
      });
    });
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* ── Toast ────────────────────────────────────────────────── */
  function toast(message, isError) {
    var el = document.querySelector(".np-toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "np-toast";
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.classList.toggle("is-error", !!isError);
    el.classList.add("is-visible");
    clearTimeout(el._hideTimer);
    el._hideTimer = setTimeout(function () { el.classList.remove("is-visible"); }, 4200);
  }

  window.NursePortal = { csrfToken: csrfToken, toast: toast };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initPickers);
  } else {
    initPickers();
  }
})();

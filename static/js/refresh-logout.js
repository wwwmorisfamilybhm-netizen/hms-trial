/*
 * Log the user out whenever the current page is reloaded (F5 / refresh button).
 *
 * Navigating between pages with links is treated normally, so the app stays
 * usable — only an actual browser reload ends the session and bounces the
 * user back to the login page.
 *
 * The logout URL is supplied by the page via <body data-logout-url="...">.
 */
(function () {
  "use strict";

  // Work out how this page was reached. The Navigation Timing API reports
  // "reload" for a refresh, "navigate" for a link/form, "back_forward" for
  // history navigation, etc.
  var navEntry = null;
  if (window.performance && performance.getEntriesByType) {
    navEntry = performance.getEntriesByType("navigation")[0] || null;
  }

  var isReload = navEntry
    ? navEntry.type === "reload"
    : !!(window.performance &&
         performance.navigation &&
         performance.navigation.type === 1); // 1 === TYPE_RELOAD (legacy)

  if (!isReload) {
    return;
  }

  var logoutUrl = document.body && document.body.getAttribute("data-logout-url");
  if (logoutUrl) {
    // replace() so the protected page is not left in the history stack.
    window.location.replace(logoutUrl);
  }
})();

// Colour theme: "auto" follows the operating system, "light" and "dark" are fixed.
// Loaded synchronously in <head> so the page never flashes in the wrong theme.
(function () {
  "use strict";

  var KEY = "mirrorhub-theme";
  var ORDER = ["auto", "light", "dark"];
  var LABELS = { auto: "Theme: auto", light: "Theme: light", dark: "Theme: dark" };

  function stored() {
    try {
      var value = window.localStorage.getItem(KEY);
      return ORDER.indexOf(value) >= 0 ? value : "auto";
    } catch (error) {
      return "auto";  // storage blocked: follow the system
    }
  }

  function apply(theme) {
    var root = document.documentElement;
    if (theme === "auto") {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", theme);
    }
  }

  function save(theme) {
    try {
      if (theme === "auto") {
        window.localStorage.removeItem(KEY);
      } else {
        window.localStorage.setItem(KEY, theme);
      }
    } catch (error) {
      // not persisted; the choice still holds for this page
    }
  }

  apply(stored());

  document.addEventListener("DOMContentLoaded", function () {
    var button = document.getElementById("theme-toggle");
    if (!button) {
      return;
    }
    var current = stored();
    button.textContent = LABELS[current];
    button.addEventListener("click", function () {
      current = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
      apply(current);
      save(current);
      button.textContent = LABELS[current];
    });
  });
})();

// Client behavior without inline scripts.
(function () {
  "use strict";

  var MAX_LOG_CHARS = 256 * 1024;
  var activeSource = null;
  var activeLog = null;
  var pendingText = "";
  var flushTimer = null;

  function flushLog() {
    if (activeLog && pendingText) {
      var combined = activeLog.textContent + pendingText;
      activeLog.textContent = combined.slice(-MAX_LOG_CHARS);
      activeLog.scrollTop = activeLog.scrollHeight;
    }
    pendingText = "";
    flushTimer = null;
  }

  function stopStream() {
    if (flushTimer !== null) {
      window.clearTimeout(flushTimer);
      flushTimer = null;
    }
    flushLog();
    if (activeSource) {
      activeSource.close();
    }
    activeSource = null;
    activeLog = null;
  }

  function setText(id, value) {
    var element = document.getElementById(id);
    if (element) {
      element.textContent = value;
    }
  }

  function startStream() {
    var element = document.getElementById("job-log");
    if (element === activeLog) {
      return;
    }

    stopStream();

    if (!element || !element.dataset.sse) {
      return;
    }

    activeLog = element;
    var source = new EventSource(element.dataset.sse);
    activeSource = source;

    source.addEventListener("log", function (event) {
      if (source !== activeSource) {
        return;
      }

      var message = JSON.parse(event.data);
      pendingText = (
        pendingText + String(message.text || "")
      ).slice(-MAX_LOG_CHARS);

      if (flushTimer === null) {
        flushTimer = window.setTimeout(flushLog, 100);
      }
    });

    source.addEventListener("status", function (event) {
      if (source !== activeSource) {
        return;
      }

      var state = JSON.parse(event.data);
      var badge = document.getElementById("job-status");
      if (badge && state.status) {
        badge.textContent = state.status;
        badge.className = "badge badge-" + state.status;
      }

      if (state.attempt !== undefined) {
        setText(
          "job-attempt",
          state.attempt + " of " + state.max_attempts
        );
      }

      var progress = document.getElementById("job-progress");
      if (progress) {
        if (state.progress_pct === null) {
          progress.removeAttribute("value");
        } else if (state.progress_pct !== undefined) {
          progress.value = state.progress_pct;
        }
      }

      setText(
        "job-progress-pct",
        state.progress_pct === null || state.progress_pct === undefined
          ? "indeterminate"
          : Math.round(state.progress_pct) + "%"
      );
      setText("job-progress-text", state.progress_text || "");
      setText(
        "job-exit-code",
        state.exit_code === null || state.exit_code === undefined
          ? "-"
          : String(state.exit_code)
      );
      setText("job-error", state.error_summary || "");

      if (state.finished_at) {
        setText("job-finished", state.finished_at + " UTC");
      }

      if (state.stream_complete) {
        stopStream();
      }
    });

    source.onerror = function () {
      // EventSource reconnects transient failures automatically.
      // HTTP 204 closes the connection permanently.
      if (
        source === activeSource &&
        source.readyState === EventSource.CLOSED
      ) {
        stopStream();
      }
    };
  }

  function bindSchedulePresets() {
    document.querySelectorAll(
      "select[name='preset_label']"
    ).forEach(function (select) {
      if (select.dataset.bound === "true") {
        return;
      }
      select.dataset.bound = "true";

      select.addEventListener("change", function () {
        var option = select.options[select.selectedIndex];
        var cron = (option.textContent || "").split(" - ")[1];
        var form = select.closest("form");
        var field = form
          ? form.querySelector("input[name='cron_expr']")
          : null;
        if (cron && field) {
          field.value = cron.trim();
        }
      });
    });
  }

  function initialize() {
    bindSchedulePresets();
    startStream();
  }

  document.addEventListener("htmx:responseError", function (event) {
    var status = event.detail.xhr.status;
    if (status === 401 || status === 403) {
      window.location.reload();
    }
  });

  document.addEventListener("htmx:afterSwap", initialize);

  document.addEventListener("htmx:beforeCleanupElement", function (event) {
    var element = event.detail.elt;
    if (
      activeLog &&
      (element === activeLog || element.contains(activeLog))
    ) {
      stopStream();
    }
  });

  window.addEventListener("pagehide", stopStream);
  window.addEventListener("pageshow", initialize);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize);
  } else {
    initialize();
  }
})();
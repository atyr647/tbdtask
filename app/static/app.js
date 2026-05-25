/**
 * tbdtask client-side helpers.
 *
 * Loaded once from base.html. Each helper looks for the elements/markers
 * it cares about and exits quietly when they're not present, so adding
 * new helpers stays cheap.
 *
 * The strict CSP blocks inline <script> and <style> blocks — everything
 * needs to live here or in a per-feature file under /static.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------
  // App shell: drawer + bottom-tab "More" trigger
  // ---------------------------------------------------------------
  function initDrawer() {
    var drawer = document.getElementById("app-drawer");
    var backdrop = document.getElementById("drawer-backdrop");
    var openBtn = document.getElementById("open-drawer");
    var closeBtn = document.getElementById("close-drawer");
    var moreBtn = document.getElementById("more-tab-btn");
    if (!drawer || !backdrop) return;

    function open(ev) {
      if (ev) ev.preventDefault();
      drawer.classList.add("open");
      backdrop.classList.add("open");
      backdrop.removeAttribute("hidden");
      drawer.setAttribute("aria-hidden", "false");
      if (openBtn) openBtn.setAttribute("aria-expanded", "true");
      document.documentElement.classList.add("no-scroll");
    }
    function close() {
      drawer.classList.remove("open");
      backdrop.classList.remove("open");
      drawer.setAttribute("aria-hidden", "true");
      if (openBtn) openBtn.setAttribute("aria-expanded", "false");
      document.documentElement.classList.remove("no-scroll");
      window.setTimeout(function () {
        if (!backdrop.classList.contains("open")) backdrop.setAttribute("hidden", "");
      }, 200);
    }

    if (openBtn) openBtn.addEventListener("click", open);
    if (closeBtn) closeBtn.addEventListener("click", close);
    backdrop.addEventListener("click", close);
    if (moreBtn) moreBtn.addEventListener("click", open);
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && drawer.classList.contains("open")) close();
    });
  }

  // ---------------------------------------------------------------
  // Date jump (Dashboard "Jump to day")
  // ---------------------------------------------------------------
  function initDayJump() {
    var input = document.getElementById("day-jump-input");
    if (!input) return;
    input.addEventListener("change", function () {
      if (input.value) window.location.href = "/day/" + input.value;
    });
  }

  // ---------------------------------------------------------------
  // Worklist filter + new-week dialog
  // ---------------------------------------------------------------
  function initWorklistList() {
    var input = document.getElementById("wl-search");
    var countEl = document.getElementById("wl-search-count");
    if (input && countEl) {
      var rows = Array.prototype.slice.call(document.querySelectorAll("tr.wl-row"));
      var groups = Array.prototype.slice.call(document.querySelectorAll("details.archive-group"));
      var initiallyOpen = groups.map(function (g) { return g.open; });
      function apply() {
        var q = input.value.trim().toLowerCase();
        if (q) groups.forEach(function (g) { g.open = true; });
        else groups.forEach(function (g, i) { g.open = initiallyOpen[i]; });
        var visible = 0;
        rows.forEach(function (row) {
          var hay = row.dataset.search || "";
          var match = !q || hay.indexOf(q) !== -1;
          row.classList.toggle("is-hidden", !match);
          if (match) visible++;
        });
        countEl.textContent = q ? (visible + " of " + rows.length + " match") : "";
      }
      input.addEventListener("input", apply);
    }

    var dialog = document.getElementById("new-week-dialog");
    if (dialog) {
      var openBtns = [
        document.getElementById("open-new-week"),
        document.getElementById("fab-new-week"),
      ];
      function openDialog(ev) {
        if (ev) ev.preventDefault();
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
      }
      function closeDialog() {
        if (typeof dialog.close === "function") dialog.close();
        else dialog.removeAttribute("open");
      }
      openBtns.forEach(function (b) { if (b) b.addEventListener("click", openDialog); });
      document.querySelectorAll("[data-close-dialog]").forEach(function (b) {
        b.addEventListener("click", closeDialog);
      });
      dialog.addEventListener("click", function (ev) {
        var r = dialog.getBoundingClientRect();
        if (ev.target === dialog) {
          var inside = ev.clientY >= r.top && ev.clientY <= r.bottom &&
                       ev.clientX >= r.left && ev.clientX <= r.right;
          if (!inside) closeDialog();
        }
      });
    }
  }

  // ---------------------------------------------------------------
  // Disclosure-focus helper:
  // any button with data-open-target="<details-id>" expands that
  // <details> and focuses its first input. Used on Quals catalog
  // for the "Add qualification" header button.
  // ---------------------------------------------------------------
  function initDisclosureTargets() {
    document.querySelectorAll("[data-open-target]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var targetId = btn.getAttribute("data-open-target");
        var target = document.getElementById(targetId);
        if (!target) return;
        target.open = true;
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        var first = target.querySelector("input, textarea, select");
        if (first) first.focus({ preventScroll: true });
      });
    });
  }

  // ---------------------------------------------------------------
  // Sole-owner danger zone countdown (admin home)
  // ---------------------------------------------------------------
  function initDeleteOrgCountdown() {
    var btn = document.getElementById("delete-org-btn");
    var countdown = document.getElementById("delete-org-countdown");
    if (!btn || !countdown) return;
    var seconds = 10;
    var timer = window.setInterval(function () {
      seconds--;
      if (seconds <= 0) {
        window.clearInterval(timer);
        btn.disabled = false;
        countdown.textContent = "You may now confirm deletion.";
        countdown.classList.add("danger-ready");
      } else {
        countdown.textContent = seconds + " seconds remaining…";
      }
    }, 1000);
  }

  // ---------------------------------------------------------------
  // Confirm prompts via data-confirm="…"
  // Works on submit buttons, plain buttons, and forms.
  // ---------------------------------------------------------------
  function initConfirmPrompts() {
    // Form-level confirm: blocks submission until user agrees.
    document.querySelectorAll("form[data-confirm]").forEach(function (f) {
      f.addEventListener("submit", function (ev) {
        var msg = f.getAttribute("data-confirm");
        if (msg && !window.confirm(msg)) ev.preventDefault();
      });
    });
    // Button-level confirm: catches clicks before form submits.
    document.querySelectorAll("button[data-confirm], a[data-confirm]").forEach(function (b) {
      b.addEventListener("click", function (ev) {
        var msg = b.getAttribute("data-confirm");
        if (msg && !window.confirm(msg)) {
          ev.preventDefault();
          ev.stopPropagation();
        }
      });
    });
  }

  // ---------------------------------------------------------------
  // Generic data-action handlers.
  //   data-action="close-details" — close nearest <details> ancestor
  // ---------------------------------------------------------------
  function initDataActions() {
    document.querySelectorAll("[data-action='close-details']").forEach(function (el) {
      el.addEventListener("click", function (ev) {
        ev.preventDefault();
        var d = el.closest("details");
        if (d) d.open = false;
      });
    });
  }

  // ---------------------------------------------------------------
  // Recurrence form: show/hide rec-blocks based on the kind <select>.
  // ---------------------------------------------------------------
  function initRecurrenceKinds() {
    var sel = document.getElementById("rec_kind");
    if (!sel) return;
    var blocks = Array.prototype.slice.call(document.querySelectorAll(".rec-block"));
    function apply() {
      var k = sel.value;
      blocks.forEach(function (b) {
        var kinds = (b.dataset.recKinds || "").split(/\s+/);
        b.classList.toggle("is-hidden", kinds.indexOf(k) === -1);
      });
    }
    sel.addEventListener("change", apply);
    apply();
  }

  // ---------------------------------------------------------------
  // Rate picker (personnel forms): category select narrows the
  // <datalist> suggestions visually.
  // ---------------------------------------------------------------
  function initRatePicker() {
    var cat = document.getElementById("rate-category");
    var list = document.getElementById("rate-suggestions");
    if (!cat || !list) return;
    var allOptions = Array.prototype.slice.call(list.querySelectorAll("option"))
      .map(function (o) { return { el: o, label: o.textContent || "" }; });
    cat.addEventListener("change", function () {
      var v = cat.value;
      allOptions.forEach(function (item) {
        if (!v) {
          item.el.disabled = false;
          item.el.classList.remove("is-hidden");
        } else {
          var match = item.label.indexOf(v) === 0;
          item.el.disabled = !match;
          item.el.classList.toggle("is-hidden", !match);
        }
      });
    });
  }

  // ---------------------------------------------------------------
  // Multi-select people combobox (worklist setup, carry-over).
  // Single-select sponsor combobox (personnel/incoming new).
  //
  // Markup contract for each [data-combo] root:
  //   input[data-combo-input][data-target-name="..."][data-combo-multi="1"|""]
  //   ul[data-combo-chips]
  //   div[data-combo-results]
  //   <script type="application/json" data-combo-data id="...">[{id,label}]</script>
  //   OR a separate <script type="application/json" id="people-data">
  //
  // The selected ids are echoed as <input type="hidden" name="<target_name>"
  // data-combo-hidden> children of the root.
  // ---------------------------------------------------------------
  function initComboboxes() {
    function dataFor(root) {
      var local = root.querySelector("script[type='application/json'][data-combo-data]");
      if (local) {
        try { return JSON.parse(local.textContent || "[]"); } catch (e) { return []; }
      }
      var srcId = root.getAttribute("data-combo-source");
      if (srcId) {
        var s = document.getElementById(srcId);
        if (s) try { return JSON.parse(s.textContent || "[]"); } catch (e) { return []; }
      }
      // Legacy fallback used by setup.html / carry_over.html / incoming_new.html.
      var fallback = document.getElementById("people-data") || document.getElementById("sponsors-data");
      if (fallback) try { return JSON.parse(fallback.textContent || "[]"); } catch (e) { return []; }
      return [];
    }

    Array.prototype.slice.call(document.querySelectorAll("[data-combo]")).forEach(function (root) {
      var input = root.querySelector("[data-combo-input]");
      var results = root.querySelector("[data-combo-results]");
      var chips = root.querySelector("[data-combo-chips]");
      if (!input || !results || !chips) return;
      var targetName = input.dataset.targetName;
      var multi = input.hasAttribute("data-combo-multi") ? true : true; // default multi
      var data = dataFor(root);
      var selected = new Map();

      function clearHidden() {
        root.querySelectorAll("input[type=hidden][data-combo-hidden]").forEach(function (n) { n.remove(); });
      }
      function render() {
        chips.innerHTML = "";
        clearHidden();
        selected.forEach(function (label, id) {
          var li = document.createElement("li");
          li.className = "combobox-chip";
          li.textContent = label + " ✕";
          li.title = "Click to remove";
          li.addEventListener("click", function () { selected.delete(id); render(); });
          chips.appendChild(li);
          var h = document.createElement("input");
          h.type = "hidden"; h.name = targetName; h.value = id;
          h.setAttribute("data-combo-hidden", "1");
          root.appendChild(h);
        });
      }
      function show(query) {
        var q = (query || "").trim().toLowerCase();
        results.innerHTML = "";
        var matches = data
          .filter(function (p) { return !selected.has(String(p.id)); })
          .filter(function (p) { return !q || p.label.toLowerCase().indexOf(q) !== -1; })
          .slice(0, 10);
        if (!matches.length) { results.classList.remove("open"); return; }
        matches.forEach(function (m) {
          var div = document.createElement("div");
          div.className = "combobox-result";
          div.textContent = m.label;
          div.addEventListener("mousedown", function (ev) {
            ev.preventDefault();
            if (!multi) selected.clear();
            selected.set(String(m.id), m.label);
            input.value = "";
            results.classList.remove("open");
            render();
            if (multi) input.focus();
          });
          results.appendChild(div);
        });
        results.classList.add("open");
      }
      input.addEventListener("focus", function () { show(input.value); });
      input.addEventListener("input", function () { show(input.value); });
      input.addEventListener("blur", function () { setTimeout(function () { results.classList.remove("open"); }, 150); });
    });
  }

  // ---------------------------------------------------------------
  // Absence calendar: search filter + click-to-pin selected row.
  // ---------------------------------------------------------------
  function initCalendarSearch() {
    var input = document.getElementById("cal-search");
    var countEl = document.getElementById("cal-search-count");
    var rows = Array.prototype.slice.call(document.querySelectorAll("tr.cal-row"));
    if (!input || !countEl || !rows.length) return;
    function apply() {
      var q = input.value.trim().toLowerCase();
      var visible = 0;
      rows.forEach(function (row) {
        var hay = row.dataset.search || "";
        var match = !q || hay.indexOf(q) !== -1;
        row.classList.toggle("is-hidden", !match);
        if (match) visible++;
      });
      countEl.textContent = q ? (visible + " of " + rows.length + " match") : "";
    }
    input.addEventListener("input", apply);
    rows.forEach(function (row) {
      row.addEventListener("click", function (ev) {
        if (ev.target.closest("a")) return;
        row.classList.toggle("selected");
      });
    });
  }

  // ---------------------------------------------------------------
  // Auto-print on /worklists/<id>/print
  // ---------------------------------------------------------------
  function initAutoPrint() {
    if (!document.body.dataset.autoPrint) return;
    window.setTimeout(function () { window.print(); }, 250);
  }

  // ---------------------------------------------------------------
  // Roster search: client-side filter for the personnel list. Walks
  // [data-roster-row] elements and matches the query against the
  // pre-computed data-search attribute. Hides per-group sections
  // whose rows are all filtered out and live-updates the count chip.
  // ---------------------------------------------------------------
  function initRosterSearch() {
    var input = document.getElementById("roster-search-input");
    if (!input) return;
    var counter = document.getElementById("roster-search-count");
    var rows = document.querySelectorAll("[data-roster-row]");
    var sections = document.querySelectorAll("[data-roster-section]");

    function apply() {
      var q = input.value.trim().toLowerCase();
      var shown = 0;
      rows.forEach(function (r) {
        var hit = !q || r.dataset.search.indexOf(q) >= 0;
        r.hidden = !hit;
        if (hit) shown += 1;
      });
      sections.forEach(function (sec) {
        var visible = 0;
        sec.querySelectorAll("[data-roster-row]").forEach(function (r) {
          if (!r.hidden) visible += 1;
        });
        sec.hidden = visible === 0;
        var c = sec.querySelector("[data-roster-section-count]");
        if (c) c.textContent = "(" + visible + ")";
      });
      if (counter) {
        counter.textContent = q
          ? shown + " match" + (shown === 1 ? "" : "es")
          : "";
      }
    }
    input.addEventListener("input", apply);
  }

  // ---------------------------------------------------------------
  // Multi-pick qual assignment: filter the checklist by typing,
  // enable/disable the submit button based on selection count, and
  // re-label the button with the current count.
  // ---------------------------------------------------------------
  function initMultiQual() {
    var form = document.querySelector("form[data-multiqual]");
    if (!form) return;
    var search = form.querySelector("[data-multiqual-search]");
    var rows = form.querySelectorAll("[data-multiqual-row]");
    var boxes = form.querySelectorAll("[data-multiqual-check]");
    var countEl = form.querySelector("[data-multiqual-count]");
    var submit = form.querySelector("[data-multiqual-submit]");

    function refresh() {
      var n = 0;
      boxes.forEach(function (b) {
        if (b.checked) n += 1;
      });
      if (countEl) {
        countEl.textContent =
          n === 1 ? "(1 selected)" : "(" + n + " selected)";
      }
      if (submit) {
        submit.disabled = n === 0;
        submit.textContent =
          n > 1 ? "Assign " + n + " qualifications" : "Assign selected";
      }
    }

    function applyFilter() {
      var q = search ? search.value.trim().toLowerCase() : "";
      rows.forEach(function (r) {
        var hit = !q || r.dataset.search.indexOf(q) >= 0;
        r.hidden = !hit;
      });
    }

    boxes.forEach(function (b) {
      b.addEventListener("change", refresh);
    });
    if (search) search.addEventListener("input", applyFilter);
    refresh();
  }

  // ---------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------
  function boot() {
    initDrawer();
    initDayJump();
    initWorklistList();
    initDisclosureTargets();
    initDeleteOrgCountdown();
    initConfirmPrompts();
    initDataActions();
    initRecurrenceKinds();
    initRatePicker();
    initComboboxes();
    initCalendarSearch();
    initAutoPrint();
    initRosterSearch();
    initMultiQual();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();

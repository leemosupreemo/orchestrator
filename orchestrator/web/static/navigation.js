(function (root) {
  "use strict";

  function mount({document, window}) {
    const app = document.querySelector(".app");
    const sidebar = document.querySelector("#sidebar");
    const toggle = document.querySelector("#nav-toggle");
    const dismiss = document.querySelector("#nav-close");
    const backdrop = document.querySelector("#sidebar-backdrop");
    const media = window.matchMedia("(max-width: 760px)");
    const background = [...document.querySelectorAll(".mobile-header, .main, .setup-fab, .setup-panel, .tabbar")];
    let opened = false, previousOverflow = "", previousInert = [];

    function close({restoreFocus = true} = {}) {
      if (!opened) return;
      opened = false;
      app.classList.remove("drawer-open");
      toggle.setAttribute("aria-expanded", "false");
      backdrop.hidden = true;
      sidebar.removeAttribute("role");
      sidebar.removeAttribute("aria-modal");
      sidebar.inert = media.matches;
      background.forEach((element, index) => { element.inert = previousInert[index]; });
      document.body.style.overflow = previousOverflow;
      if (restoreFocus && media.matches) toggle.focus();
    }

    function sync() {
      const locked = app.classList.contains("session-locked");
      if (!media.matches || locked) close({restoreFocus: false});
      sidebar.inert = media.matches && !opened;
      toggle.disabled = locked;
    }

    toggle.addEventListener("click", () => {
      if (opened) return close();
      if (!media.matches || app.classList.contains("session-locked")) return;
      opened = true;
      previousOverflow = document.body.style.overflow;
      previousInert = background.map((element) => element.inert);
      sidebar.inert = false;
      sidebar.setAttribute("role", "dialog");
      sidebar.setAttribute("aria-modal", "true");
      app.classList.add("drawer-open");
      toggle.setAttribute("aria-expanded", "true");
      backdrop.hidden = false;
      background.forEach((element) => { element.inert = true; });
      document.body.style.overflow = "hidden";
      dismiss.focus();
    });
    dismiss.addEventListener("click", () => close());
    backdrop.addEventListener("click", () => close());
    sidebar.addEventListener("click", (event) => {
      if (event.target.closest("a[href], [data-config-route], [data-action], #lock-btn, #search-btn")) close();
    });
    sidebar.addEventListener("change", (event) => {
      if (event.target.id === "project-select") close();
    });
    document.addEventListener("keydown", (event) => {
      if (!opened || event.defaultPrevented || document.querySelector("dialog[open]")) return;
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      } else if (event.key === "Tab") {
        const items = [...sidebar.querySelectorAll('a[href], button:not([disabled]), select:not([disabled]), [tabindex="0"]')]
          .filter((element) => element.getClientRects().length && !element.closest("[hidden], [inert]"));
        const first = items[0], last = items[items.length - 1];
        if (event.shiftKey && (document.activeElement === first || !sidebar.contains(document.activeElement))) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !sidebar.contains(document.activeElement))) {
          event.preventDefault();
          first?.focus();
        }
      }
    });
    window.addEventListener("hashchange", () => close());
    media.addEventListener("change", sync);
    if (window.MutationObserver) new window.MutationObserver(sync).observe(app, {attributes: true, attributeFilter: ["class"]});
    sync();
    return {close};
  }

  root.NavigationDrawer = {mount};
})(globalThis);

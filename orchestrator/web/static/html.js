(function (root) {
  "use strict";

  // The one HTML-escaping helper every page script uses for text it puts into markup. Load it first.
  const ENTITIES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};

  root.Html = {escape: (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ENTITIES[c])};
})(typeof globalThis !== "undefined" ? globalThis : window);

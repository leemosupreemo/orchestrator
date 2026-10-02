(function (root) {
  "use strict";

  // Ranks palette entries for a typed query. Higher is better; 0 means no match.
  // Order of preference: label starts with the query, a word in the label does, the label
  // contains it, the hint contains it, then the letters appear in order (so "dlv" finds Delivery).
  function score(entry, query) {
    const q = query.trim().toLowerCase();
    if (!q) return 1;
    const label = entry.label.toLowerCase();
    const hint = (entry.hint || "").toLowerCase();
    if (label.startsWith(q)) return 100 - Math.min(label.length, 50) / 100;
    if (label.split(/[\s\-/·:]+/).some((w) => w.startsWith(q))) return 80;
    if (label.includes(q)) return 60;
    if (hint.includes(q)) return 40;
    // Letters in order, starting at the beginning of a word, so "seat" doesn't match "browser alerts".
    for (let start = 0; start < label.length; start++) {
      if (label[start] !== q[0] || (start > 0 && /[a-z0-9]/.test(label[start - 1]))) continue;
      let at = start + 1, ok = true;
      for (const ch of q.slice(1)) {
        at = label.indexOf(ch, at);
        if (at === -1) { ok = false; break; }
        at += 1;
      }
      if (ok) return 20;
    }
    return 0;
  }

  function rank(entries, query, limit) {
    const max = limit || 40;
    return entries
      .map((entry, index) => ({ entry, index, s: score(entry, query) }))
      .filter((x) => x.s > 0)
      .sort((a, b) => b.s - a.s || a.entry.order - b.entry.order || a.index - b.index)
      .slice(0, max)
      .map((x) => x.entry);
  }

  const api = { score, rank };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Palette = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

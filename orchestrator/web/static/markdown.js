(function (root) {
  "use strict";

  // A small Markdown renderer for reading the product documents. Everything is HTML-escaped first and only a
  // few constructs are turned back into tags, so a document can't inject markup or scripts.
  function esc(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function safeUrl(url) {
    return /^(https?:\/\/|mailto:|#)/i.test(url.trim()) ? url.trim() : null;
  }

  function inline(raw) {
    let text = esc(raw);
    const codes = [];
    text = text.replace(/`([^`]+)`/g, (_, code) => { codes.push(`<code>${code}</code>`); return `\u0000${codes.length - 1}\u0000`; });
    text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
      const href = safeUrl(url.replace(/&amp;/g, "&"));
      return href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
    });
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[\s(])_([^_]+)_(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>").replace(/(^|[\s(])\*([^*\s][^*]*)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
    return text.replace(/\u0000(\d+)\u0000/g, (_, i) => codes[Number(i)]);
  }

  function render(markdown) {
    const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0;
    const isBlock = (l) => /^(#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s?|```|---+\s*$|\|)/.test(l);
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      let m;
      if (/^```/.test(line)) {
        const code = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
        i++;
        out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
      } else if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        const level = Math.min(m[1].length + 1, 6); // the page already has an h1
        out.push(`<h${level}>${inline(m[2])}</h${level}>`);
        i++;
      } else if (/^---+\s*$/.test(line)) {
        out.push("<hr>");
        i++;
      } else if (/^[-*+]\s+/.test(line) || /^\d+[.)]\s+/.test(line)) {
        const ordered = /^\d+[.)]\s+/.test(line);
        const top = ordered ? /^\d+[.)]\s+/ : /^[-*+]\s+/;
        const items = [];
        while (i < lines.length) {
          const cur = lines[i];
          if (top.test(cur)) items.push({ text: cur.replace(top, ""), kids: [], more: [] });
          else if (items.length && /^\s{2,}[-*+]\s+/.test(cur)) items[items.length - 1].kids.push(cur.replace(/^\s+[-*+]\s+/, "")); // one level of nesting
          else if (items.length && /^\s{2,}\S/.test(cur)) items[items.length - 1].more.push(cur.trim()); // a wrapped line of the same item
          else break;
          i++;
        }
        const body = (t) => { // "[x] text" / "[ ] text" are check boxes
          const task = t.match(/^\[( |x|X)\]\s+(.*)$/);
          return task ? `<span class="task${task[1] === " " ? "" : " done"}" role="img" aria-label="${task[1] === " " ? "not done" : "done"}">${task[1] === " " ? "☐" : "☑"}</span> ${inline(task[2])}` : inline(t);
        };
        const html = items.map((it) => `<li>${body(it.text)}${it.more.map((m) => `<br>${inline(m)}`).join("")}${it.kids.length ? `<ul>${it.kids.map((k) => `<li>${body(k)}</li>`).join("")}</ul>` : ""}</li>`);
        out.push(`<${ordered ? "ol" : "ul"}>${html.join("")}</${ordered ? "ol" : "ul"}>`);
      } else if (/^>\s?/.test(line)) {
        const quote = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) quote.push(lines[i++].replace(/^>\s?/, ""));
        out.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`);
      } else if (/^\|/.test(line) && i + 1 < lines.length && /^\|[\s:|-]+\|?\s*$/.test(lines[i + 1])) {
        const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const head = cells(line);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\|/.test(lines[i])) rows.push(cells(lines[i++]));
        out.push(`<table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
      } else {
        const para = [];
        while (i < lines.length && lines[i].trim() && !isBlock(lines[i])) para.push(lines[i++]);
        if (!para.length) { para.push(lines[i++]); }
        out.push(`<p>${inline(para.join(" "))}</p>`);
      }
    }
    return out.join("\n");
  }

  const api = { render, inline, esc };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.Markdown = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

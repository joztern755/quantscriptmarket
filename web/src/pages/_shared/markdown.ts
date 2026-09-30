// Tiny SAFE markdown renderer. Builds DOM nodes only (never innerHTML).
// Supported: # headings (1-4), paragraphs, unordered/ordered lists, **bold**, *italic* / _italic_,
// `code`, [text](url) links (http:, https:, and #/ app routes only; anything else renders as text),
// horizontal rules (---), blockquotes (> ).
// Everything else is rendered as literal text.

const SAFE_LINK = /^(https?:\/\/[^\s]+|#\/[^\s]*)$/i;

export function isSafeHref(href: string): boolean {
  return SAFE_LINK.test(href.trim());
}

/** Render inline markdown into `parent` using text nodes and a small set of elements. */
export function renderInline(parent: Node, text: string): void {
  let i = 0;
  let buf = "";
  const flush = (): void => {
    if (buf) {
      parent.appendChild(document.createTextNode(buf));
      buf = "";
    }
  };
  while (i < text.length) {
    const ch = text[i];
    // escape
    if (ch === "\\" && i + 1 < text.length && /[\\`*_\[\]()#>-]/.test(text[i + 1])) {
      buf += text[i + 1];
      i += 2;
      continue;
    }
    // inline code
    if (ch === "`") {
      const end = text.indexOf("`", i + 1);
      if (end > i + 1) {
        flush();
        const c = document.createElement("code");
        c.textContent = text.slice(i + 1, end);
        parent.appendChild(c);
        i = end + 1;
        continue;
      }
    }
    // bold
    if ((ch === "*" || ch === "_") && text[i + 1] === ch) {
      const marker = ch + ch;
      const end = text.indexOf(marker, i + 2);
      if (end > i + 2) {
        flush();
        const b = document.createElement("strong");
        renderInline(b, text.slice(i + 2, end));
        parent.appendChild(b);
        i = end + 2;
        continue;
      }
    }
    // italic
    if (ch === "*" || ch === "_") {
      const end = text.indexOf(ch, i + 1);
      // for "_" require word boundary-ish to avoid snake_case
      const prev = i > 0 ? text[i - 1] : " ";
      if (end > i + 1 && text[i + 1] !== " " && (ch === "*" || /[\s(\[]/.test(prev))) {
        flush();
        const em = document.createElement("em");
        renderInline(em, text.slice(i + 1, end));
        parent.appendChild(em);
        i = end + 1;
        continue;
      }
    }
    // link [text](href)
    if (ch === "[") {
      const close = text.indexOf("]", i + 1);
      if (close > i && text[close + 1] === "(") {
        const paren = text.indexOf(")", close + 2);
        if (paren > close) {
          const label = text.slice(i + 1, close);
          const href = text.slice(close + 2, paren).trim();
          flush();
          if (isSafeHref(href)) {
            const a = document.createElement("a");
            a.href = href; // property assignment of a validated http(s)/#/ URL
            if (/^https?:/i.test(href)) {
              a.target = "_blank";
              a.rel = "noopener noreferrer";
            }
            renderInline(a, label);
            parent.appendChild(a);
          } else {
            // unsafe scheme: render label as plain text, drop the URL
            renderInline(parent, label);
          }
          i = paren + 1;
          continue;
        }
      }
    }
    buf += ch;
    i++;
  }
  flush();
}

/** Render a markdown document to a DocumentFragment. */
export function renderMarkdown(src: string): DocumentFragment {
  const frag = document.createDocumentFragment();
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  let para: string[] = [];
  let list: HTMLOListElement | HTMLUListElement | null = null;
  let listOrdered = false;
  let quote: HTMLQuoteElement | null = null;

  const flushPara = (): void => {
    if (para.length) {
      const p = document.createElement("p");
      renderInline(p, para.join(" "));
      (quote ?? frag).appendChild(p);
      para = [];
    }
  };
  const closeList = (): void => {
    list = null;
  };
  const closeQuote = (): void => {
    quote = null;
  };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) {
      flushPara();
      closeList();
      closeQuote();
      continue;
    }
    let m: RegExpMatchArray | null;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      flushPara();
      closeList();
      closeQuote();
      const level = Math.min(4, m[1].length + 1); // # -> h2 (page has its own h1)
      const h = document.createElement(("h" + level) as "h2" | "h3" | "h4");
      renderInline(h, m[2].replace(/\s#+$/, ""));
      frag.appendChild(h);
      continue;
    }
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      flushPara();
      closeList();
      closeQuote();
      frag.appendChild(document.createElement("hr"));
      continue;
    }
    if ((m = line.match(/^>\s?(.*)$/))) {
      closeList();
      if (!quote) {
        flushPara();
        quote = document.createElement("blockquote");
        frag.appendChild(quote);
      }
      if (m[1].trim()) para.push(m[1].trim());
      else flushPara();
      continue;
    }
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      flushPara();
      closeQuote();
      const ordered = !!ol;
      if (!list || listOrdered !== ordered) {
        list = document.createElement(ordered ? "ol" : "ul");
        listOrdered = ordered;
        frag.appendChild(list);
      }
      const li = document.createElement("li");
      renderInline(li, ((ul ?? ol) as RegExpMatchArray)[1]);
      list.appendChild(li);
      continue;
    }
    if (list && /^\s{2,}\S/.test(raw) && list.lastElementChild) {
      // continuation of a list item
      list.lastElementChild.appendChild(document.createTextNode(" "));
      renderInline(list.lastElementChild, line.trim());
      continue;
    }
    closeList();
    para.push(line.trim());
  }
  flushPara();
  return frag;
}

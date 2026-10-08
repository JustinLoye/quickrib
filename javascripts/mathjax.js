// MathJax configuration for pymdownx.arithmatex in generic mode.
//
// SVG output with a *local* font cache. By default the SVG output shares one
// glyph cache, an <svg> appended to the document body, and every formula refers
// to it. mkdocs-material's instant navigation replaces the body when the reader
// follows a link, which deletes that cache and leaves every formula on the new
// page as a blank box. A local cache embeds the glyphs in each formula instead.
// The CommonHTML output has the same problem with its injected font styles.
window.MathJax = {
  tex: {
    inlineMath: [["\\(", "\\)"]],
    displayMath: [["\\[", "\\]"]],
    processEscapes: true,
    processEnvironments: true
  },
  svg: {
    fontCache: "local"
  },
  options: {
    ignoreHtmlClass: ".*|",
    processHtmlClass: "arithmatex"
  }
};

// Instant navigation swaps the page body without reloading, so the new page
// has to be typeset again. `document$` emits once per page.
document$.subscribe(() => {
  if (!window.MathJax.typesetPromise) {
    return;  // MathJax has not finished loading; its startup typesets the page.
  }
  MathJax.typesetClear();
  MathJax.texReset();
  MathJax.typesetPromise();
});

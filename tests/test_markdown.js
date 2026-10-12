"use strict";
const assert = require("node:assert/strict");
const { render } = require("../aihub/web/static/markdown.js");
assert.match(render("# Titre\n\n- un\n- deux"), /<h1>Titre<\/h1><ul><li>un<\/li><li>deux<\/li><\/ul>/);
assert.match(render("| A | B |\n| --- | ---: |\n| un | deux |"), /<table>.*<td class="align-right">deux<\/td>/);
assert.match(render("```js\n<img onerror=evil()>\n```"), /&lt;img onerror=evil\(\)&gt;/);
assert.match(render("[Site](https://example.test/)"), /href="https:\/\/example.test\/"/);
for (const url of ["javascript:evil()", "data:text/html,evil", "vbscript:evil", "javascript%3Aevil"]) {
  assert.doesNotMatch(render(`[Lien](${url})`), /href=/);
}
assert.doesNotMatch(render('<script>evil()</script><img src=x onerror="evil()">'), /<script|<img/);
assert.match(render("> Citation\n\n1. Premier\n   - enfant"), /<blockquote>.*<\/blockquote><ol.*<ul>/);
assert.doesNotThrow(() => render(">".repeat(1000) + " texte"));
assert.match(render("```\ncode en cours"), /code en cours<\/code>/);
console.log("Markdown : rendu, copie, tableaux, listes et liens non executables verifies");

// « Chercher sur ce PC » (Réglages): each « Retenir » says which copy it keeps, for a screen reader.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const slice = (from, to) => {
  const start = source.indexOf(from);
  assert.ok(start >= 0, `${from} introuvable dans app.js`);
  return source.slice(start, source.indexOf(to, start));
};

class Node {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.textContent = ''; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  addEventListener() {}
  append(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
}
const result = new Node('div');
const context = vm.createContext({
  Node, document: { createElement: (tag) => new Node(tag), createTextNode: (text) => ({ text }) },
  $: (id) => (id === 'settings-scan-result' ? result : new Node('input')), toast: () => {},
  PROGRAM_FIELDS: { claude: 'set-claude_cli', codex: 'set-codex_cli', sbx: 'set-sandbox_cli' },
});
vm.runInContext(slice('function el(', '\n}\n') + '\n}\n' + slice('function renderProgramScan(', '\nfunction setupSettingsDialog('),
  context);
context.renderProgramScan({
  complete: true, seconds: 3,
  programs: [{ program: 'codex', label: 'Codex', copies: [
    { path: 'C:\\A\\codex.exe', where: 'PATH', version: '0.163.0', in_use: true },
    { path: 'D:\\B\\codex.exe', where: 'recherche sur le PC', version: null, in_use: false }] }],
}, new Set(['codex']));

const buttons = [];
const walk = (node) => { if (node && node.tag === 'button') buttons.push(node); (node && node.children || []).forEach(walk); };
walk(result);
assert.deepEqual(buttons.map((b) => b.textContent), ['Retenir', 'Retenir', 'Revenir à la recherche automatique']);
assert.deepEqual(buttons.map((b) => b.attrs['aria-label']),
  ['Retenir C:\\A\\codex.exe', 'Retenir D:\\B\\codex.exe', 'Codex : revenir à la recherche automatique']);
console.log('Chercher sur ce PC : chaque bouton dit la copie qu’il retient');

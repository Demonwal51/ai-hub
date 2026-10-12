// Exercise the actual worker with the browser Cache API modelled in memory.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
const origin = 'https://app.test';
function worker() {
  const listeners = {}, stores = new Map();
  const key = value => new URL(typeof value === 'string' ? value : value.url, origin).href;
  const caches = {
    async open(name) {
      if (!stores.has(name)) stores.set(name, new Map());
      const entries = stores.get(name);
      return {
        async put(request, response) { entries.set(key(request), response.clone()); },
        async match(request) { return entries.get(key(request))?.clone(); },
        async delete(request) { return entries.delete(key(request)); },
        async keys() { return [...entries.keys()].map(url => new Request(url)); },
      };
    },
    async delete(name) { return stores.delete(name); },
    async keys() { return [...stores.keys()]; },
  };
  let fetcher = async () => new Response('{}');
  const self = {location:{origin},addEventListener(kind, fn){listeners[kind]=fn;}};
  vm.runInNewContext(source, {self, caches, Response, Request, Headers, URL,
    crypto: require('node:crypto').webcrypto, fetch:(...args)=>fetcher(...args)});
  return {
    caches,
    fetch(fn) { fetcher=fn; },
    async message(type) { let pending; listeners.message({data:{type},waitUntil(p){pending=p;}}); await pending; },
    async get(path) {
      let pending;
      listeners.fetch({request:new Request(origin+path),respondWith(p){pending=p;}});
      return pending;
    },
    async cached(path) { return (await caches.open('aihub-hors-ligne')).match(origin+path); },
  };
}
async function check() {
  const w = worker();
  await w.message('offline-on');
  w.fetch(async()=>new Response('private answer'));
  await w.get('/api/conversations/1?part=first');
  await w.get('/api/conversations/1?part=second');
  assert.equal(await (await w.cached('/api/conversations/1?part=first')).text(),'private answer');
  w.fetch(async()=>new Response('{}',{status:403}));
  await w.get('/api/conversations/1');
  assert.equal(await w.cached('/api/conversations/1?part=first'),undefined);
  assert.equal(await w.cached('/api/conversations/1?part=second'),undefined);

  w.fetch(async()=>new Response('{}',{status:401}));
  await w.get('/api/options');
  assert.equal(await w.cached('/__aihub-hors-ligne-active'),undefined);

  await w.message('offline-on');
  w.fetch(async()=>Response.json({required:true,logged_in:true,user:{name:'first',role:'admin'}}));
  await w.get('/api/auth/session');
  w.fetch(async()=>new Response('private first'));
  await w.get('/api/conversations/2');
  w.fetch(async()=>Response.json({required:true,logged_in:true,user:{name:'second',role:'membre'}}));
  await w.get('/api/auth/session');
  assert.equal(await w.cached('/api/conversations/2'),undefined);

  await w.message('offline-on');
  let release, sent;
  const started = new Promise(resolve=>sent=resolve);
  w.fetch(()=>{sent(); return new Promise(resolve=>release=resolve);});
  const delayed = w.get('/api/conversations/3');
  await started;
  await w.message('offline-off');
  await w.message('offline-on');
  release(new Response('secret from the former account'));
  await delayed;
  assert.equal(await w.cached('/api/conversations/3'),undefined);

  let releaseSession, sessionSent;
  const sessionStarted = new Promise(resolve=>sessionSent=resolve);
  w.fetch(()=>{sessionSent(); return new Promise(resolve=>releaseSession=resolve);});
  const oldSession = w.get('/api/auth/session');
  await sessionStarted;
  await w.message('offline-off');
  await w.message('offline-on');
  w.fetch(async()=>Response.json({required:true,logged_in:true,user:{name:'new-person',role:'membre'}}));
  await w.get('/api/auth/session');
  const newEpoch = await (await w.cached('/__aihub-hors-ligne-active')).text();
  releaseSession(Response.json({required:true,logged_in:true,user:{name:'old-person',role:'admin'}}));
  await oldSession;
  assert.equal(await (await w.cached('/__aihub-hors-ligne-active')).text(),newEpoch);
  assert.match(await (await w.cached('/__aihub-hors-ligne-compte')).text(),/new-person/);

  w.fetch(async()=>{throw new Error('offline');});
  assert.equal((await w.get('/api/conversations/3')).status,503);
  assert.equal(await w.get('/api/conversations/3/sandbox/accounts'),undefined);
  console.log('Worker checks: revocation, logout, identity, delayed response, network-only sandbox: OK');
}
check().catch(err=>{console.error(err);process.exitCode=1;});

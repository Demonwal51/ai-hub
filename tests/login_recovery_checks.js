const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('let loginDone =');
const end = source.indexOf('// --- memory: search', start);
assert.ok(end > start);
const login = source.slice(start, end);
assert.ok(login.includes('function setupLogin()'));

async function recovery(trigger) {
  const timers = new Map(), events = new Map(), nodes = new Map(), calls = [];
  let number = 0, attempts = 0;
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {disabled:false, value:'', textContent:'',
      classList:{add(){},remove(){},toggle(){}}, focus(){}, select(){},
      addEventListener(type, callback){this[type] = callback;}});
    return nodes.get(id);
  };
  const session = {required:true,logged_in:false,server_password:true};
  const context = vm.createContext({$:node, state:{session:null},
    window:{addEventListener:(name, callback)=>events.set(name,callback),removeEventListener:name=>events.delete(name)},
    setTimeout:callback=>{timers.set(++number,callback);return number;},clearTimeout:id=>timers.delete(id),
    api:async(method,path)=>{calls.push([method,path]);if (++attempts === 1) throw new Error('offline');return session;},
  });
  vm.runInContext(login,context);
  context.setupLogin();
  const result = context.loadLoginSession();
  await new Promise(setImmediate);
  assert.equal(node('login-submit').disabled,false);
  assert.equal(node('login-submit').textContent,'Réessayer la connexion');
  if (trigger === 'click') {
    await node('login-form').submit({preventDefault(){}});
    // Another immediate submit must not send a password while rechecking.
    await node('login-form').submit({preventDefault(){}});
  } else if (trigger === 'online') events.get('online')();
  else timers.values().next().value();
  assert.equal(await result,session);
  assert.equal(node('login-submit').disabled,false);
  assert.equal(node('login-submit').textContent,'Se connecter');
  assert.ok(calls.every(([method,path])=>method==='GET' && path==='/api/auth/session'));
  assert.equal(timers.size,0);
  assert.equal(events.size,0);
}

(async()=>{
  for (const trigger of ['click','online','timer']) await recovery(trigger);
  const timers = new Map(); let number = 0;
  const context = vm.createContext({AbortController,
    setTimeout:callback=>{timers.set(++number,callback);return number;},clearTimeout:id=>timers.delete(id),
    fetch:(_path,options)=>new Promise((_resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted')))),
  });
  vm.runInContext(source.slice(source.indexOf('async function api('),source.indexOf('\nfunction el(')),context);
  const request = context.api('POST','/api/auth/login',{});
  timers.values().next().value();
  await assert.rejects(request,/Le serveur ne répond pas/);
  assert.equal(timers.size,0);
  console.log('Login recovery: click, online, timer, no password replay and bounded request passed');
})().catch(error=>{console.error(error);process.exitCode=1;});

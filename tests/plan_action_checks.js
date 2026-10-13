const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const action = source.slice(source.indexOf('async function planAction('), source.indexOf('\nfunction agentOptions('));
assert.ok(action.startsWith('async function planAction('));

(async () => {
  const state = { currentId: 3, data: { plans: [{ id: 7, status: 'proposed' }] } };
  let rendered = 0;
  const context = vm.createContext({ state,
    api: async () => ({ plan: { id: 7, status: 'stopped' } }),
    renderConversation: () => { rendered++; assert.equal(state.data.plans[0].status, 'stopped'); },
    refreshConversation: async () => { throw new Error('offline'); },
    toast: () => {},
  });
  vm.runInContext(action, context);
  await context.planAction('POST', '/api/plans/7/cancel');
  assert.equal(state.data.plans[0].status, 'stopped');
  assert.equal(rendered, 1);

  let complete;
  context.api = () => new Promise(resolve => { complete = resolve; });
  context.refreshConversation = async () => {};
  state.data.plans = [{ id: 7, status: 'proposed' }];
  const pending = context.planAction('POST', '/api/plans/7/cancel');
  state.currentId = 4;
  complete({ plan: { id: 7, status: 'stopped' } });
  await pending;
  assert.equal(state.data.plans[0].status, 'proposed');
  assert.equal(rendered, 1);
  console.log('Plan cancellation: offline refresh and navigation checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

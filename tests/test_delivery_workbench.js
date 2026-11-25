const fs = require('fs'), vm = require('vm'), assert = require('assert');
class E {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.value=''; this.hidden=false; this.disabled=false; this.textContent=''; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children=[...items]; }
  setAttribute() {}
  addEventListener() {}
}
const nodes=new Map();
const document={getElementById(id){ if(!nodes.has(id)) nodes.set(id,new E()); return nodes.get(id); }, createElement(tag){return new E(tag);}};
let requests=[];
const fetch=async (url, options={}) => { requests.push({url,options}); return {ok:true,status:200,json:async()=>options.method==='POST'?{id:'request-1',status:'queued'}:{tasks:[]}}; };
const context=vm.createContext({document,fetch,crypto:{randomUUID:()=> 'unique-id'},console,Map,Set,URL,EventSource:function(){}});
let source=fs.readFileSync('collaboration_runtime/static/workbench.js','utf8').replace('loadTeams().catch((e) => notice(e.message));','');
vm.runInContext(source, context);
(async()=>{
  await vm.runInContext(`(async()=>{
    state.team='team'; state.actor='local-operator'; state.generation=1;
    state.deliveryTasks=[{id:'task12345',goal_id:'goal',goal_status:'active',owner_id:'owner',reviewer_id:'reviewer',revision:2,status:'ready',request:null}];
    renderDeliveries();
    const row=$('delivery-list').children[0];
    if (row.children[0].textContent.indexOf('task1234')<0) throw Error('missing task');
    const fields=row.children.filter(x=>x.tag==='textarea' || x.tag==='input');
    fields[0].value='out/a.txt\\nout/b.txt'; fields[0].oninput(); fields[1].value='check'; fields[1].oninput();
    await row.children.find(x=>x.tag==='button').onclick();
  })()`,context);
  const post=requests.find(r=>r.options.method==='POST');
  const body=JSON.parse(post.options.body);
  assert.deepStrictEqual(body.paths,['out/a.txt','out/b.txt']); assert.strictEqual(body.expected_revision,2); assert.strictEqual(body.note,'check'); assert.strictEqual(body.request_id,'unique-id');
  assert(post.url.endsWith('/teams/team/tasks/task12345/delivery-requests'));
  vm.runInContext(`state.actor='member'; renderDeliveries(); if (!$('delivery-section').hidden) throw Error('member can see operator area');`,context);
  console.log('delivery workbench smoke: PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});

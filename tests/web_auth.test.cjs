const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/veyquant/web/app.js'), 'utf8');

const response = (status, data) => ({ok:status < 400, status, json:async () => data});
const statusData = () => ({csrf_token:'test-csrf', broker_connected:true,
  new_proposals_stopped:false, session_expires_at:Date.now()/1000 + 7*86400,
  analysis:{state:'observing', daily_jobs:2, daily_limit:12, reports:[]}});
const denied = () => response(401, {error:'invalid_session'});
const flush = async () => { for (let i=0; i<5; i++) await new Promise(setImmediate); };

function app(handler) {
  const elements = new Map(), events = {}, timers = new Map(), calls = [];
  let sequence = 0;
  function element(id) {
    if (!elements.has(id)) elements.set(id, {hidden:['dashboard','onboarding'].includes(id),
      value:'', textContent:'', disabled:false, children:[], listeners:{}, attrs:{},
      set id(value) { elements.set(value,this); },
      setAttribute(name,value) {this.attrs[name]=value;}, removeAttribute(name) {delete this.attrs[name];},
      addEventListener(name, fn) { this.listeners[name] = fn; },
      replaceChildren() { this.children = []; }, append(...items) { this.children.push(...items); }});
    return elements.get(id);
  }
  const document = {getElementById:element, createElement:tag => element('node'+(++sequence)),
    visibilityState:'visible', addEventListener:(name, fn) => { events[name] = fn; }};
  const tg = {initData:'fixture-launch', ready(){}, expand(){}, onEvent:(name, fn) => {events[name] = fn;}};
  const context = {document, AbortController, crypto:require('node:crypto').webcrypto, window:{Telegram:{WebApp:tg}, addEventListener:(name, fn) => {events[name] = fn;}},
    setTimeout:fn => {const id=++sequence; timers.set(id, fn); return id;},
    clearTimeout:id => timers.delete(id), fetch:async (url, options) => {
      calls.push({url, options}); return handler(url, options);
    }};
  vm.runInNewContext(source, context);
  return {element, events, calls, timers,
    createdCount:()=>elements.size,
    renderFull(value,role="all"){context.fixture=value;context.fixtureRole=role;vm.runInNewContext('renderFullDecision(document.getElementById("large-record"),fixture,fixtureRole)',context);delete context.fixture;},
    async poll() {
    const [id, callback] = [...timers.entries()].at(-1);
    timers.delete(id); await callback(); await flush();
  }, async click(id) { await element(id).listeners.click(); await flush(); }};
}

test('opening and reopening restores an existing cookie without consuming Telegram launch data', async () => {
  for (let i=0; i<2; i++) {
    const a=app(async url => {assert.equal(url, '/v1/status'); return response(200, statusData());});
    await flush();
    assert.equal(a.element('dashboard').hidden, false);
    assert.equal(a.element('login').hidden, true);
    assert.equal(a.calls.length, 1);
    assert.equal(a.timers.size, 1);
  }
});

test('missing cookie triggers automatic owner login, with no setup code', async () => {
  let cookie=false;
  const a=app(async (url, options) => {
    if (url==='/v1/status') return cookie ? response(200,statusData()) : denied();
    assert.equal(url, '/v1/auth/login');
    assert.deepEqual(JSON.parse(options.body), {init_data:'fixture-launch'});
    cookie=true; return response(200, {authenticated:true});
  });
  await flush();
  assert.equal(a.element('dashboard').hidden, false);
  assert.equal(a.element('onboarding').hidden, true);
  assert.equal(a.calls.filter(c=>c.url==='/v1/auth/login').length, 1);
});

test('network failure and HTTP 503 preserve the dashboard and recover without logging in again', async () => {
  let failure=0;
  const a=app(async url => {
    assert.equal(url, '/v1/status');
    if (failure===1) throw new TypeError('network fixture');
    return failure===2 ? response(503,{error:'temporarily_unavailable'}) : response(200,statusData());
  });
  await flush();
  for (failure of [1,2]) {
    await a.poll();
    assert.equal(a.element('dashboard').hidden, false);
    assert.match(a.element('notice').textContent, /다시 연결/);
  }
  failure=0; await a.poll();
  assert.equal(a.element('notice').textContent, '');
  assert.equal(a.timers.size, 1);
});

test('simultaneous page and Telegram foreground events share one login request', async () => {
  let release, cookie=false;
  const gate=new Promise(resolve=>{release=resolve;});
  const a=app(async url => {
    if(url==='/v1/status') {await gate; return cookie ? response(200,statusData()) : denied();}
    cookie=true; return response(200,{});
  });
  a.events.pageshow(); a.events.activated(); a.events.visibilitychange();
  release(); await flush();
  assert.equal(a.calls.filter(c=>c.url==='/v1/auth/login').length,1);
  assert.equal(a.timers.size,1);
});

test('setup code appears only for an unbound account and remains visible after foregrounding', async () => {
  const a=app(async url => url==='/v1/status' ? denied() : response(401,{error:'binding_required'}));
  await flush();
  assert.equal(a.element('onboarding').hidden,false);
  a.events.activated(); await flush();
  assert.equal(a.element('onboarding').hidden,false);
  assert.equal(a.calls.filter(c=>c.url==='/v1/auth/login').length,1);
});

test('an old entered setup code cannot reroute an existing owner into binding', async () => {
  let online=false, cookie=false;
  const a=app(async url => {
    if(!online) throw new TypeError('network fixture');
    if(url==='/v1/status') return cookie ? response(200,statusData()) : denied();
    assert.equal(url,'/v1/auth/login'); cookie=true; return response(200,{});
  });
  await flush(); a.element('invitation').value='old-used-code'; online=true;
  await a.click('connect');
  assert.equal(a.element('dashboard').hidden,false);
  assert.equal(a.element('invitation').value,'');
  assert.equal(a.calls.filter(c=>c.url==='/v1/auth/bind').length,0);
});

test('expired Telegram launch explains reopening without presenting a password error', async () => {
  const a=app(async url => url==='/v1/status' ? denied() : response(401,{error:'telegram_auth_expired'}));
  await flush();
  assert.equal(a.element('onboarding').hidden,true);
  assert.match(a.element('notice').textContent,/코드는 필요하지 않습니다/);
  assert.equal(a.timers.size,0);
});

test('a still-valid legacy session renews with CSRF instead of reusing initData', async () => {
  const a=app(async (url,options) => {
    if(url==='/v1/status') return response(200,{...statusData(), session_expires_at:Date.now()/1000+100});
    assert.equal(url,'/v1/auth/renew');
    assert.equal(options.headers['X-Veyquant-CSRF'],'test-csrf');
    return response(200,{csrf_token:'test-csrf'});
  });
  await flush();
  assert.equal(a.element('dashboard').hidden,false);
  assert.equal(a.calls.length,2);
});

test('explicit session revocation suppresses automatic login and foreground recovery', async () => {
  const a=app(async url => response(200,url==='/v1/status' ? statusData() : {sessions_revoked:true}));
  await flush(); await a.click('revoke');
  const count=a.calls.length;
  a.events.activated(); a.events.online(); await flush();
  assert.equal(a.calls.length,count);
  assert.equal(a.element('dashboard').hidden,true);
  assert.equal(a.timers.size,0);
});


const selected = {cheap:'au.anthropic.claude-haiku-4-5-20251001-v1:0',middle:'au.anthropic.claude-sonnet-4-6',research:'au.anthropic.claude-opus-4-6-v1'};
const strategy={preset:'stable',prompt:'안정적 관찰 기준'};
const modelData = {models:Object.entries(selected).map(([role,id])=>({id,name:role,description:role,reasoning_options:["none","low","medium","high"],default_reasoning:"none"})),strategy_presets:[{id:'stable',name:'안정',description:'안정 관찰',prompt:strategy.prompt}]};
const limits = {capital_krw:'1000000',max_order_krw:'100000',max_daily_loss_krw:'50000'};
const policyFixture = (revision=1) => ({revision,configured:true,onboarding_completed:true,models:{...selected},limits:{...limits},strategy:{...strategy}});
const withPolicy = policy => ({...statusData(),operating_policy:policy,model_catalog:modelData});

test('first onboarding has four steps, validates amounts, saves models and opens home', async()=>{
  let saved;
  const initial={...policyFixture(0),configured:false,onboarding_completed:false,limits:null};
  const a=app(async(url,options)=>{
    if(url==='/v1/status')return response(200,withPolicy(initial));
    assert.equal(url,'/v1/settings');saved=JSON.parse(options.body);
    return response(200,{operating_policy:policyFixture(1)});
  });
  await flush();
  assert.equal(a.element('setup-screen').hidden,false);
  assert.equal(a.element('step-1').hidden,false);
  await a.click('setup-next');assert.equal(a.element('step-2').hidden,false);
  await a.click('setup-next');assert.match(a.element('policy-notice').textContent,/1원/);
  for(const [key,value] of Object.entries(limits)){a.element(key).value=value;a.element(key).listeners.input();}
  assert.match(a.element('capital-help').textContent,/100만/);
  await a.click('setup-next');assert.equal(a.element('step-3').hidden,false);
  await a.click('setup-next');assert.equal(a.element('step-4').hidden,false);
  await a.click('save-settings');
  assert.deepEqual(saved,{cheap_reasoning:"low",middle_reasoning:"medium",research_reasoning:"high",live_requested:"false",expected_revision:'0',strategy_preset:strategy.preset,strategy_prompt:strategy.prompt,...limits,cheap_model:selected.cheap,middle_model:selected.middle,research_model:selected.research});
  assert.equal(a.element('home-screen').hidden,false);
  assert.equal(a.element('bottom-nav').hidden,false);
});

test('saved settings open home and model changes use a review step', async()=>{
  let sent;
  const a=app(async(url,options)=>{
    if(url==='/v1/status')return response(200,withPolicy(policyFixture()));
    sent=JSON.parse(options.body);
    return response(200,{operating_policy:{...policyFixture(2),models:{...selected,cheap:selected.research}}});
  });
  await flush();assert.equal(a.element('home-screen').hidden,false);
  await a.click('nav-settings');await a.click('edit-models');
  a.element('cheap_model').value=selected.research;a.element('cheap_model').listeners.change();
  await a.click('setup-next');assert.equal(a.element('step-4').hidden,false);
  await a.click('save-settings');
  assert.equal(sent.cheap_model,selected.research);assert.equal(sent.capital_krw,'1000000');
  assert.equal(a.element('settings-screen').hidden,false);
});

test('polling and navigation preserve drafts; conflict requires latest settings reload', async()=>{
  let policy=policyFixture(),saved=false;
  const a=app(async(url,options)=>{
    if(url==='/v1/status')return response(200,withPolicy(policy));
    assert.equal(JSON.parse(options.body).expected_revision,'1');saved=true;
    return response(409,{error:'policy_changed'});
  });
  await flush();await a.click('edit-limits');
  a.element('capital_krw').value='2000000';a.element('capital_krw').listeners.input();
  await a.poll();assert.equal(a.element('capital_krw').value,'2000000');
  await a.click('setup-close');await a.click('edit-limits');
  assert.equal(a.element('capital_krw').value,'2000000');
  policy={...policyFixture(2),limits:{...limits,capital_krw:'3000000'}};
  await a.poll();assert.equal(a.element('capital_krw').value,'2000000');
  await a.click('setup-next');await a.click('save-settings');assert.equal(saved,true);
  assert.equal(a.element('reload-policy').hidden,false);
  await a.click('reload-policy');assert.equal(a.element('capital_krw').value,'3,000,000');
});

test('failed settings save retains values and invalid money never reaches API', async()=>{
  let posts=0;
  const a=app(async url=>{
    if(url==='/v1/status')return response(200,withPolicy(policyFixture()));
    posts++;throw new TypeError('offline');
  });
  await flush();a.element('capital_krw').value='1e8';a.element('capital_krw').listeners.input();
  await a.click('save-settings');assert.equal(posts,0);
  a.element('capital_krw').value='2,000,000';
  await a.click('save-settings');assert.equal(posts,1);
  assert.equal(a.element('capital_krw').value,'2,000,000');
  assert.match(a.element('policy-notice').textContent,/입력값을 유지/);
  assert.equal(a.element('save-settings').disabled,false);
});

const claudeModels={cheap:'au.anthropic.claude-haiku-4-5-20251001-v1:0',middle:'au.anthropic.claude-sonnet-4-6',research:'au.anthropic.claude-opus-4-6-v1'};
const gptModels={cheap:'gpt-5.6-luna',middle:'gpt-5.6-terra',research:'gpt-5.6-sol'};
const extendedCatalog={...modelData,
  models:[...modelData.models,...Object.values(claudeModels).map(id=>({id,name:id,ready:true})),...Object.values(gptModels).map(id=>({id,name:id,ready:false}))],
  presets:[{id:'chatgpt',name:'ChatGPT',models:gptModels,ready:false},{id:'claude',name:'Claude',models:claudeModels,ready:true}],
  limit_presets:[{id:'medium',name:'500만원',limits:{capital_krw:'5000000',max_order_krw:'500000',max_daily_loss_krw:'50000'}}],
  strategy_presets:[...modelData.strategy_presets,{id:'active',name:'적극',prompt:'기회와 위험 비교'},{id:'aggressive',name:'공격',prompt:'변화를 빠르게 검토'}]};
const extendedStatus=policy=>({...withPolicy(policy),model_catalog:extendedCatalog});
const textOf=element=>element.textContent+element.children.map(textOf).join(' ');

test('presets fill reviewed amounts and exact models only on user choice; nothing is posted early',async()=>{
  const initial={...policyFixture(0),configured:false,onboarding_completed:false,limits:null};
  const a=app(async url=>{assert.equal(url,'/v1/status');return response(200,extendedStatus(initial));});
  await flush();assert.equal(a.element('capital_krw').value,'');
  await a.click('model-preset-chatgpt');
  assert.equal(a.element('research_model').value,gptModels.research);
  assert.match(a.element('model-connection-note').textContent,/API 키/);
  await a.click('model-preset-claude');
  assert.equal(a.element('cheap_model').value,claudeModels.cheap);
  assert.equal(a.element('model-connection-note').textContent,'');
  await a.click('model-preset-custom');
  assert.equal(a.element('custom-models').open,true);
  assert.equal(a.element('model-preset-custom').attrs['aria-pressed'],'true');
  await a.click('setup-next');await a.click('limit-preset-medium');
  assert.equal(a.element('capital_krw').value,'5,000,000');
  assert.equal(a.element('max_order_krw').value,'500,000');
  a.element('max_order_krw').value='250000';a.element('max_order_krw').listeners.input();
  assert.equal(a.element('limit-preset-custom').attrs['aria-pressed'],'true');
  assert.equal(a.element('capital_krw').value,'5,000,000');
  assert.equal(a.calls.filter(c=>c.options.method==='POST').length,0);
});

test('custom strategy draft survives preset comparison, polling and settings save preserves money',async()=>{
  let saved;
  const a=app(async(url,options)=>{
    if(url==='/v1/status')return response(200,extendedStatus(policyFixture()));
    saved=JSON.parse(options.body);return response(200,{operating_policy:{...policyFixture(2),strategy:{preset:saved.strategy_preset,prompt:saved.strategy_prompt}}});
  });
  await flush();await a.click('edit-strategy');await a.click('strategy-preset-aggressive');
  assert.equal(a.element('strategy_prompt').readOnly,true);
  await a.click('customize-strategy');
  assert.equal(a.element('strategy_prompt').value,'변화를 빠르게 검토');
  a.element('strategy_prompt').value='배당 지속성 확인 <script>literal</script>';
  a.element('strategy_prompt').listeners.input();
  await a.click('strategy-preset-active');await a.click('strategy-preset-custom');
  await a.poll();assert.equal(a.element('strategy_prompt').value,'배당 지속성 확인 <script>literal</script>');
  await a.click('setup-next');assert.equal(a.element('step-4').hidden,false);
  assert.equal(a.element('review-strategy-prompt').textContent,'배당 지속성 확인 <script>literal</script>');
  await a.click('save-settings');
  assert.equal(saved.strategy_preset,'custom');assert.equal(saved.capital_krw,limits.capital_krw);
  assert.equal(saved.cheap_model,selected.cheap);
});

test('stage filters distinguish actual decisions, uncalled stages and legacy missing summaries',async()=>{
  const report={event_id:'fixture',name:'삼성전자',created_at:123,quote:{price:'70000',currency:'KRW',as_of:123},outcome:'watch',summary:'종합 관찰',evidence:[],stages:[
    {role:'cheap',model:selected.cheap,status:'received',decision:{action:'escalate',summary:'첫 번째 판단'}},
    {role:'middle',model:selected.middle,status:'received',decision:{action:'escalate',summary:'두 번째 판단'}},
    {role:'research',model:selected.research,status:'received',decision:{action:'watch',summary:'세 번째 판단',counterargument:'반대 증거'}}]};
  let reports=[report];
  const a=app(async()=>response(200,{...extendedStatus(policyFixture()),analysis:{...statusData().analysis,reports}}));
  await flush();assert.match(textOf(a.element('reports')),/첫 번째 판단/);
  await a.click('filter-middle');
  assert.match(textOf(a.element('reports')),/두 번째 판단/);
  assert.doesNotMatch(textOf(a.element('reports')),/첫 번째 판단|세 번째 판단/);
  reports=[{...report,event_id:'short',outcome:'no_action',stages:report.stages.slice(0,1)}];
  await a.poll();await a.click('filter-research');
  assert.match(textOf(a.element('reports')),/미실행/);
  assert.doesNotMatch(textOf(a.element('reports')),/세 번째 판단/);
  reports=[{...report,event_id:'legacy',stages:[{role:'cheap',model:selected.cheap,status:'received'}]}];
  await a.poll();await a.click('filter-cheap');
  assert.match(textOf(a.element('reports')),/판단 요약이 저장되지/);
});

test('provider keys clear before sending; success and failure never overwrite setting drafts',async()=>{
  let release, fail=false, connected=false;
  const gate=new Promise(resolve=>{release=resolve;});
  const a=app(async(url,options)=>{
    if(url==='/v1/status') return response(200,{...extendedStatus(policyFixture(2)),provider_connections:{openai:{connected,models:Object.values(gptModels),verified_at:connected ? 1000 : null}}});
    assert.equal(url,'/v1/providers/connect');
    const body=JSON.parse(options.body); assert.equal(body.provider,'openai'); assert.equal(body.api_key,'fixture_key_only_not_real_12345');
    assert.equal(options.headers['X-Veyquant-CSRF'],'test-csrf');
    assert.equal(a.element('provider-api-key').value,''); await gate;
    if(fail) return response(422,{error:'provider_connection_failed'});
    connected=true; return response(200,{provider_connections:{openai:{connected:true,models:Object.values(gptModels),verified_at:1000}}});
  });
  await flush(); await a.click('edit-models'); await a.click('model-preset-chatgpt');
  a.element('capital_krw').value='2,345,000';a.element('capital_krw').listeners.input();
  a.element('provider-api-key').value='fixture_key_only_not_real_12345';
  const pending=a.click('connect-provider'); await flush();
  assert.equal(a.element('provider-api-key').value,''); assert.equal(a.element('connect-provider').disabled,true);
  release(); await pending;
  assert.match(a.element('provider-status').textContent,/키 저장됨/);
  assert.equal(a.element('capital_krw').value,'2,345,000');
  assert.equal(a.element('research_model').value,gptModels.research);
  fail=true;a.element('provider-api-key').value='fixture_key_only_not_real_12345';await a.click('connect-provider');
  assert.match(a.element('provider-notice').textContent,/기존 연결은 유지/);
  assert.match(a.element('provider-status').textContent,/키 저장됨/);
  assert.equal(a.element('provider-api-key').value,'');
  assert.equal(a.calls.filter(c=>c.url==='/v1/settings').length,0);
});

test('provider switch, navigation and logout clear unsubmitted key',async()=>{
  const a=app(async(url)=>response(200,extendedStatus(policyFixture(1)))); await flush();
  for(const action of ['provider-gemini','nav-settings','revoke']) {
    a.element('provider-api-key').value='fixture_key_only_not_real_12345'; await a.click(action);
    assert.equal(a.element('provider-api-key').value,'');
  }
});

test('reasoning draft survives polling and is sent separately for every role',async()=>{
  let sent;
  const a=app(async(url,options)=>{
    if(url==='/v1/status')return response(200,withPolicy(policyFixture()));
    sent=JSON.parse(options.body);return response(200,{operating_policy:policyFixture(2)});
  });
  await flush();await a.click('edit-models');
  a.element('research_reasoning').value='high';a.element('research_reasoning').listeners.change();
  await a.poll();assert.equal(a.element('research_reasoning').value,'high');
  await a.click('setup-next');await a.click('save-settings');
  assert.equal(sent.research_reasoning,'high');assert.equal(sent.cheap_reasoning,'low');
});

test('inactive banner enables a pending preference and bottom control cancels it',async()=>{
  let policy={...policyFixture(),live_requested:false,live_enabled:false,live_message:'실행기 연결 대기'};
  const a=app(async(url,options)=>{
    if(url==='/v1/status')return response(200,withPolicy(policy));
    assert.equal(url,'/v1/live-preference');
    const data=JSON.parse(options.body);assert.equal(data.expected_revision,String(policy.revision));
    policy={...policy,revision:policy.revision+1,live_requested:data.enabled==='true'};
    return response(200,{operating_policy:policy});
  });
  await flush();assert.equal(a.element('live-banner').hidden,false);assert.equal(a.element('live-bottom').hidden,true);
  await a.click('enable-live');assert.equal(a.element('live-banner').hidden,true);
  assert.equal(a.element('live-bottom').hidden,false);assert.equal(a.element('enable-live').hidden,true);
  await a.click('disable-live');assert.equal(a.element('enable-live').hidden,false);assert.equal(a.element('live-bottom').hidden,true);
  policy={...policy,live_requested:true,live_enabled:true};await a.poll();
  assert.equal(a.element('live-banner').hidden,true);assert.equal(a.element('disable-live').textContent,'실거래 비활성화');
  policy={...policy,live_enabled:false};await a.poll();assert.equal(a.element('live-banner').hidden,true);
});

test('execution command result remains visible after status polling clears connection notices',async()=>{
  let done=false;
  const order={id:'vq'+'a'.repeat(32),state:'ACKNOWLEDGED',symbol:'005930',side:'BUY',quantity:3,limit_price:'70000',filled_quantity:1,filled_amount:'70000',created_at:1000};
  const a=app(async url=>{
    if(url==='/v1/status') return response(200,{...withPolicy(policyFixture()),execution:{state:'active',available:true,message:'운용 중',orders:[order],managed_positions:[],command_results:done?[{id:'request-1',result:'cancel_requested'}]:[]}});
    assert.equal(url,'/v1/execution-command'); done=true; return response(200,{request_id:'request-1'});
  });
  await flush();
  const card=a.element('execution-orders').children[0];
  const button=card.children.find(n=>n.textContent==='남은 수량 취소');
  await button.listeners.click();await flush();
  assert.match(a.element('execution-result').textContent,/취소를 요청했습니다/);
  await a.poll();assert.match(a.element('execution-result').textContent,/취소를 요청했습니다/);
});

test('domestic market navigation searches by symbol, paginates and preserves drafts on polling', async () => {
  const data=statusData(); data.operating_policy=policyFixture(); data.model_catalog=modelData;
  data.universe={available:true,catalogue_fresh:true,total:2900,covered:2800,watch_count:49,state:'partial_prices',scan_at:1000};
  const a=app(async url=>{
    if(url==='/v1/status') return response(200,data);
    assert.match(url,/^\/v1\/universe\?/);
    return response(200,{available:true,catalogue_fresh:true,matched:81,has_next:true,scan_at:1000,items:[{symbol:'0101N0',name:'테스트 우선주',market:'KOSDAQ',common_share:false,eligibility:'eligible',watched:true,price:'70000',as_of:1000}]});
  });
  await flush();
  await a.click('nav-market');
  assert.equal(a.element('market-screen').hidden,false);
  assert.equal(a.element('home-screen').hidden,true);
  assert.match(a.element('universe-summary').textContent,/2,900종목/);
  a.element('stock-search').value='0101N0';
  await a.element('stock-market').listeners.change(); await flush();
  assert.match(a.calls.at(-1).url,/q=0101N0/);
  await a.click('market-next');
  assert.match(a.calls.at(-1).url,/page=1/);
  await a.poll();
  assert.equal(a.element('stock-search').value,'0101N0');
  await a.click('nav-home'); assert.equal(a.element('market-screen').hidden,true);
});

test('different positions and orders retain their own names and quantities', async () => {
  const data=statusData(); data.execution={available:true,orders:[{id:'vqfixture',symbol:'000660',name:'SK하이닉스',side:'BUY',quantity:2,limit_price:'100000',filled_quantity:2,filled_amount:'200000',state:'FILLED',created_at:1000}],managed_positions:[{symbol:'000660',name:'SK하이닉스',quantity:2},{symbol:'005930',name:'삼성전자',quantity:5}]};
  const a=app(async()=>response(200,data)); await flush();
  assert.equal(a.element('execution-shares').textContent,'2종목');
  assert.equal(a.element('managed-positions').children.length,2);
  assert.match(a.element('execution-orders').children[0].children[1].textContent,/SK하이닉스 · 000660/);
});

const decisionStatus=()=>({...statusData(),analysis:{protocol:'decision-v2',state:'observing',active:false,orders_blocked:false,runs:[],sources:{web:'connected',dart:'not_connected'},schedule:[],surveillance:[]}});
test('manual instruction remains after busy rejection and a new attempt gets a new id', async()=>{
 let accept=false;const requests=[];
 const a=app(async(url,options)=>{
  if(url==='/v1/status') return response(200,decisionStatus());
  if(url==='/v1/analysis/manual') {requests.push(JSON.parse(options.body));return response(accept?202:409,{accepted:accept});}
  throw new Error(url);
 });
 await flush();a.element('manual-instruction').value='삼성전자를 검토해줘';
 await a.click('submit-instruction');assert.equal(a.element('manual-instruction').value,'삼성전자를 검토해줘');
 accept=true;await a.click('submit-instruction');
 assert.notEqual(requests[0].request_id,requests[1].request_id);assert.equal(a.element('manual-instruction').value,'');
 assert.match(a.element('manual-result').textContent,/판단을 시작/);
});
test('manual retries after uncertain transport retain idempotency id', async()=>{
 const ids=[];
 const a=app(async(url,options)=>{
  if(url==='/v1/status') return response(200,decisionStatus());
  ids.push(JSON.parse(options.body).request_id);throw new TypeError('fixture offline');
 });
 await flush();a.element('manual-instruction').value='검토 제안';
 await a.click('submit-instruction');await a.click('submit-instruction');
 assert.equal(ids[0],ids[1]);assert.equal(a.element('manual-instruction').value,'검토 제안');
});
test('pipeline state disables busy requests and AWS search needs no key',async()=>{
 const data=decisionStatus();data.analysis.active=true;data.analysis.sources={web:'connected'};
 const a=app(async()=>response(200,data));await flush();
 assert.equal(a.element('submit-instruction').disabled,true);
 assert.match(a.element('web-search-status').textContent,/웹검색 · 정리.*의사결정/);
 await a.click('home-dart');assert.match(a.element('optional-help').textContent,/공시/);
});
test('large analysis creates tables only when opened and pages the market table',async()=>{
 const a=app(async()=>response(200,statusData()));await flush();
 const rows=Array.from({length:2764},(_,i)=>[String(i).padStart(6,'0'),'종목','KOSPI','eligible','100',1000]);
 const bars=Array.from({length:200},()=>({date:'2026-09-09',open:'100',high:'100',low:'100',close:'100',volume:'10'}));
 const evidence=Array.from({length:200},(_,i)=>({source:'fixture',symbol:String(i).padStart(6,'0'),daily_bars:bars}));
 const before=a.createdCount();a.renderFull({initial_context:{cash:'1000',holdings:[],comparison_table:{rows}},evidence});
 assert.ok(a.createdCount()-before<700,'closed sections must not create hundreds of thousands of nodes');
 const archive=a.element('large-record').children.find(s=>s.children[0]?.textContent==='전체 계층의 보관 원본 자료');
 assert.equal(archive.children.length,1,'raw archive remains lazy until explicitly opened');
 archive.open=true;archive.listeners.toggle();
 const root=archive;
 const compare=root.children.find(s=>s.children[0]?.textContent.startsWith('전체 시장 비교표'));
 assert.equal(compare.children.length,1);compare.open=true;compare.listeners.toggle();
 const table=compare.children[1].children[0];assert.equal(table.children.length,101);
 compare.children[2].listeners.click();assert.equal(table.children.length,201);
 compare.listeners.toggle();assert.equal(table.children.length,201,'reopening must not duplicate the table');
 const stock=root.children.find(s=>s.children[0]?.textContent==='fixture · 000000');
 assert.equal(stock.children.length,1);stock.open=true;stock.listeners.toggle();
 assert.ok(stock.children.length>1);
});

test('analysis separates exact model input from stored raw data and provider token usage',async()=>{
 const a=app(async()=>response(200,statusData()));await flush();
 const payload={brief:'compact_input_fixture'};
 const measurement={payload_bytes:1000,system_bytes:500,schema_bytes:500,total_bytes:2000,budget_bytes:48000,fields:{brief:{bytes:1000}}};
 a.renderFull({
   trace:[{role:'research',task:'investment_decision',status:'received',provider_called:true,input_context:measurement,input_tokens:9000,output_tokens:100}],
   model_inputs:[{role:'research',task:'investment_decision',payload}],
   evidence:[{id:'raw',source:'fixture',excerpt:'raw_archive_fixture'}],
 });
 const root=a.element('large-record'), input=root.children.find(s=>s.children[0]?.textContent==='AI에 전달한 입력');
 const text=n=>[n.textContent,...n.children.map(text)].join(' ');
 assert.match(text(input),/의사결정 · 1회 · 입력 합계 2 KB/);
 assert.match(text(input),/호출당 상한 48 KB/);
 assert.match(text(input),/입력 9,000 · 출력 100 tokens/);
 assert.doesNotMatch(text(input),/compact_input_fixture|raw_archive_fixture/,'request payload stays collapsed');
 const call=input.children.find(s=>s.children[0]?.textContent==='1. 의사결정 · 최종 판단');
 const snapshot=call.children.find(s=>s.children[0]?.textContent==='AI 입력 요청 내용');
 snapshot.open=true;snapshot.listeners.toggle();
 assert.match(text(snapshot),/compact_input_fixture/);
 assert.doesNotMatch(text(input),/raw_archive_fixture/);
 const archive=root.children.find(s=>s.children[0]?.textContent==='전체 계층의 보관 원본 자료');
 assert.equal(archive.children.length,1);
});

test('blocked model input is shown as uncalled and legacy records never imply zero measured input',async()=>{
 const a=app(async()=>response(200,statusData()));await flush();
 const text=n=>[n.textContent,...n.children.map(text)].join(' ');
 a.renderFull({trace:[{role:'research',task:'investment_decision',provider_called:false,input_context:{total_bytes:49000,budget_bytes:48000}}],model_inputs:[{role:'research',task:'investment_decision',payload:{brief:'blocked'}}]});
 assert.match(text(a.element('large-record')),/의사결정 · 0회 · 입력 합계 0 B · 호출 전 차단 1회/);
 assert.match(text(a.element('large-record')),/AI 호출 전에 차단/);
 assert.match(text(a.element('large-record')),/차단된 입력 내용/);
 a.renderFull({trace:[{role:'middle',task:'review_batch',input_tokens:100,output_tokens:10}]});
 assert.match(text(a.element('large-record')),/입력 크기가 저장되지 않았습니다/);
 assert.doesNotMatch(text(a.element('large-record')),/입력 합계 0/);
});

test('optional data connection lives on home and settings, clears keys and disconnects',async()=>{
 const data=withPolicy(policyFixture());data.provider_connections={dart:{connected:false},krx:{connected:false}};
 const a=app(async(url,options)=>{
   if(url==='/v1/providers/connect') {const input=JSON.parse(options.body);assert.equal(input.provider,'krx');assert.equal(a.element('optional-api-key').value,'');data.provider_connections.krx={connected:true};return response(200,{provider_connections:data.provider_connections});}
   if(url==='/v1/providers/disconnect') {assert.equal(JSON.parse(options.body).provider,'krx');data.provider_connections.krx={connected:false};return response(200,{provider_connections:data.provider_connections});}
   return response(200,data);
 });await flush();await a.click('home-krx');assert.equal(a.element('optional-panel').hidden,false);
 assert.match(a.element('optional-help').textContent,/두 API/);
 a.element('optional-api-key').value='fixture_key_not_real_123456';await a.click('connect-optional');
 assert.equal(a.element('optional-api-key').value,'');assert.equal(a.element('disconnect-krx').hidden,false);
 await a.click('nav-settings');await a.click('disconnect-krx');assert.equal(a.element('disconnect-krx').hidden,true);
 assert.match(a.element('optional-settings-notice').textContent,/기본 분석/);
});
test('onboarding contains no DART or KRX key inputs',()=>{
 const html=fs.readFileSync(path.join(__dirname,'../src/veyquant/web/index.html'),'utf8');
 const step=html.slice(html.indexOf('<section id="step-1"'),html.indexOf('<section id="step-2"'));
 assert.doesNotMatch(step,/provider-dart|optional-api-key|home-krx/);
});

test('role prompts preserve drafts, save all roles and never submit the immutable harness', async()=>{
  const defaults={cheap:'감시 기본',middle:'정리 기본',research:'판단 기본'};
  const harnesses={cheap:'감시 고정 가이드',middle:'정리 고정 가이드',research:'판단 고정 가이드'};
  let saved;
  const policy={...policyFixture(),prompts:{...defaults}};
  const data={...withPolicy(policy),model_catalog:{...modelData,role_prompts:{defaults,harnesses,max_chars:2000}}};
  const a=app(async(url,options)=>{
    if(url==='/v1/status')return response(200,data);
    saved=JSON.parse(options.body);
    return response(200,{operating_policy:{...policy,revision:2,prompts:{cheap:saved.cheap_prompt,middle:saved.middle_prompt,research:saved.research_prompt}}});
  });
  await flush();await a.click('edit-role-prompts');
  assert.equal(a.element('role-prompts-editor').open,true);
  for(const role of Object.keys(defaults)){
    assert.equal(a.element(role+'_prompt').value,defaults[role]);
    assert.equal(a.element(role+'-harness').textContent,harnesses[role]);
    a.element(role+'_prompt').value='수정 '+role;
    a.element(role+'_prompt').listeners.input();
  }
  await a.poll();
  assert.equal(a.element('research_prompt').value,'수정 research');
  await a.click('reset-cheap-prompt');
  assert.equal(a.element('cheap_prompt').value,defaults.cheap);
  assert.equal(saved,undefined);
  await a.click('setup-next');await a.click('save-settings');
  assert.equal(saved.cheap_prompt,defaults.cheap);
  assert.equal(saved.middle_prompt,'수정 middle');
  assert.equal(saved.research_prompt,'수정 research');
  assert.equal(Object.keys(saved).some(key=>key.includes('harness')),false);
  const html=fs.readFileSync(path.join(__dirname,'../src/veyquant/web/index.html'),'utf8');
  for(const role of Object.keys(defaults)) assert.match(html,new RegExp('<p[^>]*id="'+role+'-harness"'));
});

test('DART failures explain the actual cause without blaming every error on the key',async()=>{
 for(const [code,expected] of [['dart_ip_denied',/접속 IP/],['dart_maintenance',/점검/],['dart_connection_unavailable',/통신하지 못했습니다/],['dart_key_unregistered',/등록되지 않은/]]){
  const data=withPolicy(policyFixture());data.provider_connections={dart:{connected:false}};
  const a=app(async url=>url==='/v1/status'?response(200,data):response(422,{error:code}));
  await flush();await a.click('home-dart');a.element('optional-api-key').value='fixture_key_not_real_123456';await a.click('connect-optional');
  assert.match(a.element('optional-notice').textContent,expected);
  assert.match(a.element('optional-notice').textContent,/저장하지 않았습니다/);
  assert.equal(a.element('optional-api-key').value,'');
  assert.equal(a.element('connect-optional').disabled,false);
 }
});

test('memory book shows committed content as text and labels the initial legacy seed',async()=>{
  const data=statusData();
  data.analysis={protocol:'decision-v2',state:'observing',runs:[],sources:{},schedule:[],
    memory_book:{revision:2,origin:'decision_model',content:'<img src=x onerror=alert(1)>\n매수는 제안, 체결 미확인',updated_at:Date.now()/1000}};
  const a=app(async()=>response(200,data));await flush();
  assert.equal(a.element('memory-book-content').textContent,data.analysis.memory_book.content);
  assert.match(a.element('memory-book-version').textContent,/2번째/);
  assert.match(a.element('memory-book-updated').textContent,/최근 갱신/);
  data.analysis.memory_book={revision:0,origin:'legacy_seed',content:'이전 짧은 요약'};
  await a.poll();assert.match(a.element('memory-book-updated').textContent,/다음 의사결정/);
  const html=fs.readFileSync(path.join(__dirname,'../src/veyquant/web/index.html'),'utf8');
  assert.match(html,/<p[^>]*id="memory-book-content"/);
});

test('input overflow offers a deliberate retry, preserves id after transport failure and deduplicates clicks',async()=>{
 const data=statusData(), id='a'.repeat(32), sent=[];
 data.analysis={protocol:'decision-v2',state:'observing',active:false,orders_blocked:false,sources:{},
   runs:[{id,kind:'scheduled',state:'aborted',started:Date.now()/1000,error:'input_budget_exceeded'}]};
 const a=app(async(url,options)=>{
  if(url==='/v1/status') return response(200,data);
  assert.equal(url,`/v1/analysis/${id}/retry-input-limit`);
  sent.push(JSON.parse(options.body));
  if(sent.length===1) throw new Error('lost response');
  return response(202,{accepted:true,id:'b'.repeat(32),duplicate:true});
 });await flush();
 assert.equal(sent.length,0);
 assert.equal(a.element(`retry-input-${id}`).textContent,'입력 한도 없이 다시 판단');
 await a.click(`retry-input-${id}`);
 assert.match(a.element(`retry-input-result-${id}`).textContent,/같은 요청/);
 await a.poll();await a.click(`retry-input-${id}`);
 assert.equal(sent[0].request_id,sent[1].request_id);
 assert.deepEqual(Object.keys(sent[0]),['request_id']);
 assert.equal(a.element(`retry-input-${id}`).disabled,true);
 await a.click(`retry-input-${id}`);assert.equal(sent.length,2);
});

test('decision detail is readable as plain text and older records remain supported',async()=>{
 const a=app(async()=>response(200,statusData()));await flush();
 const explanation='핵심 근거\n<script>not executable</script>\n관망 이유와 무효화 조건';
 a.renderFull({decision:{summary:'관망',detailed_explanation:explanation}});
 const root=a.element('large-record');
 const paragraph=root.children.find(n=>n.textContent===explanation);
 assert.ok(paragraph);
 assert.equal(paragraph.className,'prompt-preview');
 assert.ok(root.children.some(n=>n.textContent==='의사결정 상세 설명'));
 a.renderFull({decision:{summary:'과거 판단'}});
 assert.ok(!root.children.some(n=>n.textContent==='의사결정 상세 설명'));
});

test('middle filter cannot present the final decision explanation or memory as its own output',async()=>{
 const a=app(async()=>response(200,statusData()));await flush();
 a.renderFull({decision:{detailed_explanation:'FINAL_ONLY'},memory_book_after:{content:'MEMORY_ONLY'},trace:[{role:'middle',decision:{summary:'MIDDLE_ONLY'}},{role:'research',decision:{summary:'FINAL_TRACE'}}]},'middle');
 const text=n=>[n.textContent,...n.children.map(text)].join(' ');
 assert.match(text(a.element('large-record')),/MIDDLE_ONLY/);
 assert.doesNotMatch(text(a.element('large-record')),/FINAL_ONLY|MEMORY_ONLY|FINAL_TRACE/);
});

test('summary cards visibly separate middle evidence from final decision and preserve role filters',async()=>{
 const data=statusData();data.analysis={protocol:'decision-v2',runs:[{id:'layer-fixture',kind:'scheduled',state:'complete',started:1,brief:{severity:'WARN',summary:'MIDDLE_EVIDENCE',candidates:[]},decision:{action:'NO_ACTION',summary:'FINAL_CONCLUSION',counterargument:'反証',uncertainty:'未確認',intents:[]}}]};
 const a=app(async()=>response(200,data));await flush();
 const text=n=>[n.textContent,...n.children.map(text)].join(' ');
 const reports=a.element('reports');const card=reports.children.find(n=>n.children.some(c=>c.className==='analysis-stage analysis-middle'));
 assert.ok(card);
 const middle=card.children.find(n=>n.className==='analysis-stage analysis-middle');
 const finalCard=reports.children.find(n=>n.className==='report layer-research');
 assert.notEqual(card,finalCard);
 assert.ok(card.children.every(n=>n.className!=='analysis-stage analysis-research'));
 const final=finalCard.children.find(n=>n.className==='analysis-stage analysis-research');
 assert.match(text(middle),/정리 계층 · 근거와 후보.*MIDDLE_EVIDENCE/);
 assert.doesNotMatch(text(middle),/FINAL_CONCLUSION/);
 assert.match(text(final),/의사결정 계층 · 최종 결론.*FINAL_CONCLUSION/);
 assert.doesNotMatch(text(final),/MIDDLE_EVIDENCE/);
 await a.click('filter-middle');assert.match(text(reports),/MIDDLE_EVIDENCE/);assert.doesNotMatch(text(reports),/FINAL_CONCLUSION/);
 await a.click('filter-research');assert.match(text(reports),/FINAL_CONCLUSION/);assert.doesNotMatch(text(reports),/MIDDLE_EVIDENCE/);
});

test('deferred critical reviews never appear as final investment decisions, including legacy NO_ACTION',async()=>{
 const text=n=>[n.textContent,...n.children.map(text)].join(' ');
 for(const legacy of [false,true]) {
  const data=statusData();data.analysis={protocol:'decision-v2',runs:[{id:'deferred',kind:'critical',state:'complete',started:1,brief:{severity:'WARN',summary:'MIDDLE_DEFER',candidates:[],handoff:'deferred'},trace:[{role:'middle',task:'review_batch'}],...(legacy ? {decision:{action:'NO_ACTION',summary:'SYNTHETIC_FINAL',intents:[]}} : {})}]};
  const a=app(async()=>response(200,data));await flush();
  assert.match(text(a.element('reports')),/정리에서 종료/);
  assert.match(text(a.element('reports')),/의사결정 AI 미호출/);
  assert.doesNotMatch(text(a.element('reports')),/의사결정 계층 · 최종 결론|SYNTHETIC_FINAL/);
  await a.click('filter-research');
  assert.ok(a.element('reports').children.every(n=>n.className!=='report layer-research'));
  assert.doesNotMatch(text(a.element('reports')),/MIDDLE_DEFER|SYNTHETIC_FINAL/);
 }
});

test('both saved optional connections hide the home banner and remain in settings',async()=>{
 const data=statusData();data.provider_connections={dart:{connected:true},krx:{connected:true}};
 data.analysis={protocol:'decision-v2',runs:[],layers:[],sources:{dart:'unavailable',krx:'connected'}};
 const a=app(async()=>response(200,data));await flush();
 assert.equal(a.element('optional-banner').hidden,true);
 assert.equal(a.element('disconnect-dart').hidden,false);
 assert.equal(a.element('disconnect-krx').hidden,false);
 data.provider_connections.dart.connected=false;await a.poll();
 assert.equal(a.element('optional-banner').hidden,false);
});

test('P&L holds the last finite same-day result during loading and clears it on logout',async()=>{
 const data=statusData();data.execution={available:true,loss:{pnl_krw:'12345'}};
 let fail=false;
 const a=app(async()=>{if(fail) throw new Error('network');return response(200,data);});await flush();
 assert.equal(a.element('execution-pnl').textContent,'12,345원');
 assert.equal(a.element('execution-pnl-loading').hidden,true);
 data.execution.loss=null;await a.poll();
 assert.equal(a.element('execution-pnl').textContent,'12,345원');
 assert.equal(a.element('execution-pnl-loading').hidden,false);
 data.execution.loss={pnl_krw:'NaN'};await a.poll();
 assert.equal(a.element('execution-pnl').textContent,'12,345원');
 fail=true;await a.poll();assert.equal(a.element('execution-pnl').textContent,'12,345원');
 fail=false;data.execution.loss={pnl_krw:'0'};await a.poll();
 assert.equal(a.element('execution-pnl').textContent,'0원');
 assert.equal(a.element('execution-pnl-loading').hidden,true);
 await a.click('revoke');assert.equal(a.element('execution-pnl').textContent,'—');
});

test('independent server layer records use separate cards and role-specific detail requests',async()=>{
 const id='f'.repeat(32),data=statusData();data.analysis={protocol:'decision-v2',runs:[],layers:[
 {id:id+':middle',run_id:id,role:'middle',kind:'scheduled',started:1,state:'complete',brief:{severity:'WARN',summary:'REVIEW_OK',candidates:[]}},
 {id:id+':research',run_id:id,role:'research',kind:'scheduled',started:2,state:'aborted',error:'provider_call_failed',trace:[]},
 {id:'watch:cheap',role:'cheap',started:3,state:'complete',severity:'WARN',summary:'WATCH_ONLY'}]};
 const a=app(async url=>url.includes('/layers/') ? response(200,{data:{role:url.split('/').at(-1),trace:[]}}) : response(200,data));await flush();
 const cards=a.element('reports').children.filter(n=>n.className?.startsWith('report layer-'));
 assert.deepEqual(cards.map(n=>n.className),['report layer-cheap','report layer-research','report layer-middle']);
 const text=n=>[n.textContent,...n.children.map(text)].join(' ');
 assert.doesNotMatch(text(cards[2]),/provider_call_failed|이번 판단으로 새 주문/);
 const button=cards[2].children.find(n=>n.textContent==='정리 상세와 근거 보기');
 await button.listeners.click();assert.ok(a.calls.some(c=>c.url===`/v1/analysis/${id}/layers/middle`));
});

test('previous-day P&L is not displayed as today on first load',async()=>{
 const data=statusData();data.execution={available:true,loss:{day:'2020-01-01',pnl_krw:'50000'}};
 const a=app(async()=>response(200,data));await flush();
 assert.equal(a.element('execution-pnl').textContent,'—');
 assert.equal(a.element('execution-pnl-loading').hidden,false);
});

test('older independent records stay visible across refresh and clear on logout',async()=>{
 const data=statusData();
 const record=(id,started)=>({id,role:'cheap',started,state:'complete',severity:'NORMAL',summary:id});
 data.analysis={protocol:'decision-v2',runs:[],layers:[record('new:cheap',100)],history_cursor:'first'};
 const a=app(async url=>url==='/v1/analysis/history?cursor=first' ? response(200,{records:[record('old:cheap',1)],next_cursor:null}) : response(200,data));
 await flush();await a.click('load-older-analysis');
 const ids=()=>a.element('reports').children.map(n=>n.attrs['data-layer-id']).filter(Boolean);
 assert.deepEqual(ids(),['new:cheap','old:cheap']);
 assert.equal(a.element('load-older-analysis').hidden,true);
 await a.poll();assert.deepEqual(ids(),['new:cheap','old:cheap']);
 await a.click('revoke');assert.equal(a.element('reports').children.length,0);
});

test('hero uses confirmed P&L and real layer freshness, preserving values on network failure',async()=>{
  const data=withPolicy({...policyFixture(),live_enabled:true,live_requested:true});
  data.execution={available:true,state:'active',updated_at:Date.now()/1000,loss:{pnl_krw:'900'},monthly_performance:{state:'available',month:new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit'}).format(new Date()),pnl_krw:'125000'},managed_positions:[],orders:[]};
  data.analysis={protocol:'decision-v2',updated_at:Date.now()/1000,state:'analyzing',active:true,layers:[{id:'hero:middle',role:'middle',state:'running',started:1}],schedule:[]};
  let fail=false;
  const a=app(async()=>{if(fail)throw new TypeError('offline');return response(200,data);});
  await flush();
  assert.equal(a.element('hero-pnl').textContent,'+125,000원');
  assert.equal(a.element('hero-middle').attrs['data-active'],'true');
  assert.equal(a.element('hero-research').attrs['data-active'],'false');
  data.analysis.updated_at-=600;await a.poll();
  assert.equal(a.element('hero-middle').attrs['data-active'],'false');
  fail=true;await a.poll();
  assert.equal(a.element('hero-status').textContent,'다시 연결 중');
  assert.equal(a.element('hero-pnl').textContent,'+125,000원');
  assert.equal(a.element('hero-pnl-loading').hidden,false);
});

test('hero distinguishes losses, observation and pause; its shortcuts never submit a judgment',async()=>{
  const data=withPolicy({...policyFixture(),live_enabled:false,live_requested:false});
  data.execution={available:true,state:'disabled',loss:{pnl_krw:'900'},monthly_performance:{state:'available',month:new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit'}).format(new Date()),pnl_krw:'-5000'},managed_positions:[],orders:[]};
  const a=app(async()=>response(200,data));await flush();
  assert.equal(a.element('hero-pnl').textContent,'-5,000원');
  assert.equal(a.element('hero-pnl').attrs['data-direction'],'negative');
  assert.equal(a.element('hero-status').textContent,'관찰 모드');
  await a.click('hero-performance');assert.equal(a.element('order-history').open,true);
  await a.click('hero-research');assert.equal(a.element('reports-screen').hidden,false);
  await a.click('hero-strategy');assert.equal(a.element('step-3').hidden,false);
  data.new_proposals_stopped=true;await a.poll();
  assert.equal(a.element('hero-status').textContent,'운용 일시 중단');
  assert.equal(a.element('hero-next').textContent,'운용 재개 후');
  assert.equal(a.calls.filter(c=>c.options.method==='POST').length,0);
});

test('hero never substitutes daily P&L or a previous month for monthly performance',async()=>{
  const data=withPolicy(policyFixture());
  data.execution={available:true,loss:{pnl_krw:'999999'},monthly_performance:{state:'available',month:'2000-01',pnl_krw:'888888'},managed_positions:[],orders:[]};
  const a=app(async()=>response(200,data));await flush();
  assert.equal(a.element('hero-pnl').textContent,'—');
  assert.equal(a.element('hero-pnl-loading').hidden,false);
  data.execution.monthly_performance={state:'month_baseline_required'};await a.poll();
  assert.equal(a.element('hero-pnl').textContent,'—');
  assert.match(a.element('hero-pnl-note').textContent,/월초 평가 기준/);
});

test('new installation connects the broker before model onboarding and clears submitted keys',async()=>{
 const data=withPolicy({...policyFixture(),onboarding_completed:false});
 data.installation={ip:'15.134.164.178',broker_configured:false};
 const a=app(async(url,options)=>{if(url==='/v1/installation/broker'){const body=JSON.parse(options.body);assert.equal(body.client_secret,'example-secret');assert.equal(body.account_seq,undefined);data.installation.broker_configured=true;return response(200,{configured:true});}return response(200,data);});
 await flush();assert.equal(a.element('broker-screen').hidden,false);assert.equal(a.element('setup-screen').hidden,true);
 a.element('broker-client-id').value='example-id';a.element('broker-client-secret').value='example-secret';
 await a.click('connect-broker');
 assert.equal(a.element('broker-client-secret').value,'');assert.equal(a.element('broker-screen').hidden,true);assert.equal(a.element('step-1').hidden,false);
 assert.equal(a.calls.filter(c=>c.options.method==='POST').length,1);
});

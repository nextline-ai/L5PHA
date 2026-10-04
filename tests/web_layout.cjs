const {chromium,webkit}=require('playwright');
const http=require('node:http'),fs=require('node:fs'),assert=require('node:assert/strict');
const dir=process.cwd()+'/src/veyquant/web', out=process.cwd()+'/var/l5pha-preview';
const {execFileSync}=require('node:child_process');
fs.mkdirSync(out,{recursive:true});
const catalog=JSON.parse(execFileSync(process.env.UI_TEST_PYTHON || '.venv/bin/python',['-c','import json; from veyquant.model_catalog import catalog_view; print(json.dumps(catalog_view()))'],{env:{...process.env,PYTHONPATH:'src'},encoding:'utf8'}));
for(const model of catalog.models) model.ready=true;
for(const preset of catalog.presets) preset.ready=true;
const now=Date.now()/1000;
const preset=catalog.presets.find(p=>p.id==='chatgpt');
const status={csrf_token:'fixture',broker_connected:true,new_proposals_stopped:false,mode:'live',session_expires_at:now+604800,
 model_catalog:catalog,provider_connections:{openai:{connected:true},gemini:{connected:true},dart:{connected:true},krx:{connected:true}},
 operating_policy:{revision:1,onboarding_completed:true,configured:true,live_enabled:true,live_requested:true,live_message:'설정한 한도 안에서 자동 운용합니다.',models:preset.models,reasoning:preset.reasoning,
 limits:{capital_krw:'10000000',max_order_krw:'3000000',max_daily_loss_krw:'1000000'},strategy:{preset:'stable',prompt:catalog.strategy_presets[0].prompt},updated_at:now},
 account:{state:'fresh',domestic_market_value_krw:'12384500',cash_buying_power_krw:'4615500',snapshot_at:now,open_orders:0,conditional_orders:0},
 collector:{snapshot_at:now,frames_received:15608},readiness:{checks:[{label:'계좌 연결',passed:true},{label:'운용 한도 설정',passed:true}]},
 execution:{available:true,state:'active',message:'설정한 전략과 한도 안에서 운용하고 있습니다.',updated_at:now,loss:{pnl_krw:'184500'},monthly_performance:{state:'available',month:new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit'}).format(new Date()),pnl_krw:'284500',as_of:now},managed_positions:[{symbol:'005930',name:'삼성전자',quantity:20},{symbol:'000660',name:'SK하이닉스',quantity:5}],orders:[]},
 universe:{available:true,catalogue_fresh:true,total:2765,covered:2765,watch_count:200,scan_at:now},
 analysis:{protocol:'decision-v2',updated_at:now,state:'observing',active:false,orders_blocked:false,sources:{web:'connected',dart:'connected',web_provider:'OpenAI',decision_web_provider:'OpenAI'},usage:{groups:[]},history_cursor:'older',schedule:[{at:now+7200}],layers:[
 {id:'a:research',run_id:'a',role:'research',started:now,state:'complete',kind:'scheduled',decision:{action:'NO_ACTION',summary:'현재 보유 비중을 유지합니다.',counterargument:'단기 상승 기회를 놓칠 수 있습니다.',uncertainty:'다음 거래일 흐름은 확인되지 않았습니다.',intents:[]}},
 {id:'a:middle',run_id:'a',role:'middle',started:now-80,state:'complete',kind:'scheduled',brief:{severity:'WARN',summary:'반도체 업종의 거래량이 늘었습니다. 보유 종목을 포함해 8개 후보를 검토했습니다.',candidates:[{symbol:'005930',reason:'보유 종목 · 거래량 변화'}]}},
 {id:'a:cheap',role:'cheap',started:now-200,state:'complete',severity:'NORMAL',summary:'가격과 거래량이 일반적인 범위에 있습니다.'}
 ]}};
(async()=>{
 const server=http.createServer((req,res)=>{const file=req.url==='/'?'index.html':req.url.slice(1);if(!['index.html','app.css','app.js'].includes(file)){res.writeHead(404);res.end();return;}res.setHeader('Content-Type',file.endsWith('css')?'text/css':file.endsWith('js')?'text/javascript':'text/html');res.end(fs.readFileSync(dir+'/'+file));});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const url='http://127.0.0.1:'+server.address().port;
 try {
 for(const engine of [chromium,webkit]) {
 fs.mkdirSync(out+'/'+engine.name(),{recursive:true});
 const browser=await engine.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:390,height:844},deviceScaleFactor:1});
  await page.addInitScript(()=>{window.Telegram={WebApp:{ready(){},expand(){},onEvent(){},setHeaderColor(){},setBackgroundColor(){},setBottomBarColor(){}}};});
  let currentStatus=structuredClone(status);
  const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.route('https://telegram.org/**',r=>r.fulfill({body:''}));
  await page.route('**/v1/**',async r=>{
   if(r.request().method()!=='GET') throw Error('Unexpected mutation '+r.request().url());
   const body=r.request().url().includes('/universe')?{available:true,catalogue_fresh:true,matched:2,scan_at:now,has_next:false,items:[{symbol:'005930',name:'삼성전자',market:'KOSPI',price:'70500',as_of:now,watched:true},{symbol:'000660',name:'SK하이닉스',market:'KOSPI',price:'194500',as_of:now,watched:true}]}:currentStatus;
   await r.fulfill({json:body});
  });
  await page.goto(url);await page.waitForSelector('#home-screen:not([hidden])');
  assert.equal(await page.locator('#hero-pnl').innerText(),'+284,500원');
  await page.emulateMedia({reducedMotion:'reduce'});
  assert.equal(await page.locator('.autonomy-core svg').evaluate(e=>getComputedStyle(e).animationName),'none');
  await page.emulateMedia({reducedMotion:'no-preference'});
  assert.equal(await page.locator('.autonomy-core svg').evaluate(e=>getComputedStyle(e).animationName),'core-drift');
  await page.screenshot({path:out+'/'+engine.name()+'/home-mobile.png',fullPage:true});
  await page.locator('#autonomy-hero').screenshot({path:out+'/'+engine.name()+'/hero-mobile.png'});
  await page.locator('#hero-performance').click();assert.equal(await page.locator('#order-history').evaluate(e=>e.open),true);
  await page.locator('#hero-research').click();assert.equal(await page.locator('.report').count(),1);
  await page.locator('#filter-all').click();await page.locator('#nav-home').click();
  await page.locator('#hero-strategy').click();assert.equal(await page.locator('#step-3').isVisible(),true);
  await page.locator('#setup-close').click();await page.locator('#nav-home').click();
  assert.equal(await page.locator('#optional-banner').isVisible(),false);
  assert.equal(await page.evaluate(()=>getComputedStyle(document.documentElement).backgroundColor),'rgb(255, 255, 255)');
  assert.equal(await page.locator('.brand').innerText().then(t=>t.includes('L5PHA')),true);
  for(const view of ['reports','market','settings']) {
   await page.locator('#nav-'+view).click();
   if(view==='reports') {
    assert.equal(await page.locator('.report').count(),3);
    await page.locator('#filter-research').click();assert.equal(await page.locator('.report').count(),1);
    await page.locator('#filter-all').click();
    await page.locator('.report').first().locator('summary').first().click();
   }
   await page.screenshot({path:out+'/'+engine.name()+'/'+view+'-mobile.png',fullPage:true});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,view+' overflows');
  }
  await page.locator('#edit-models').click();
  await page.screenshot({path:out+'/'+engine.name()+'/models-mobile.png',fullPage:true});
  await page.locator('#model-preset-custom').click();
  assert.equal(await page.locator('#cheap_model').isVisible(),true);
  await page.locator('#cheap_reasoning').selectOption('high');
  assert.equal(await page.locator('#cheap_reasoning').inputValue(),'high');
  await page.locator('#setup-close').click();await page.locator('#edit-limits').click();await page.screenshot({path:out+'/'+engine.name()+'/limits-mobile.png',fullPage:true});
  await page.locator('#setup-close').click();await page.locator('#edit-strategy').click();await page.screenshot({path:out+'/'+engine.name()+'/strategy-mobile.png',fullPage:true});
  await page.locator('#setup-close').click();await page.locator('#nav-home').click();
  for(const width of [320,768,1024,1440]) {await page.setViewportSize({width,height:1024});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'home overflow '+width);await page.screenshot({path:out+'/'+engine.name()+'/home-'+width+'.png',fullPage:true});}
  currentStatus.provider_connections.krx.connected=false;
  await page.reload();await page.waitForSelector('#optional-banner:not([hidden])');
  currentStatus.operating_policy={...currentStatus.operating_policy,onboarding_completed:false,configured:false,live_enabled:false,live_requested:false,limits:null,revision:0};
  await page.reload();await page.waitForSelector('#setup-screen:not([hidden])');
  await page.setViewportSize({width:320,height:740});
  await page.locator('#model-preset-chatgpt').click();await page.locator('#setup-next').click();
  await page.locator('#limit-preset-medium').click();await page.locator('#setup-next').click();
  await page.locator('#strategy-preset-active').click();await page.locator('#setup-next').click();
  assert.equal(await page.locator('#live_requested').inputValue(),'false');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'onboarding overflow');
  await page.screenshot({path:out+'/'+engine.name()+'/onboarding-review.png',fullPage:true});
  await page.unroute('**/v1/**');await page.route('**/v1/**',r=>r.fulfill({status:401,json:{error:'invalid_session'}}));await page.setViewportSize({width:390,height:844});await page.reload();await page.waitForTimeout(100);await page.screenshot({path:out+'/'+engine.name()+'/login-mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);console.log(engine.name()+': layout, navigation, independent reports and optional banner passed');
 } finally {await browser.close();}
 }
 } finally {server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

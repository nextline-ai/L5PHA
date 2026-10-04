const {chromium,webkit}=require('playwright');
const fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
const crypto=require('node:crypto');
(async()=>{
 const root=process.cwd();fs.mkdirSync(root+'/var/installer-preview',{recursive:true});
 const server=http.createServer((req,res)=>{
  const files={'/install':'installer/index.html','/install/install.js':'installer/install.js','/install/install.css':'installer/install.css','/app.css':'src/veyquant/web/app.css'};
  if(req.url==='/install/release.json'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({version:'test',region:'ap-southeast-2',template_url:'https://example.s3.ap-southeast-2.amazonaws.com/test/install.json'}));return;}
  if(!files[req.url]){res.writeHead(404);res.end();return;}
  res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript':req.url.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(root+'/'+files[req.url]));
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 try{for(const engine of [chromium,webkit]){const browser=await engine.launch();try{
  const page=await browser.newPage({viewport:{width:390,height:844}});const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.goto('http://127.0.0.1:'+server.address().port+'/install');await page.waitForFunction(()=>document.getElementById('install-status').textContent.includes('Sydney'));
  assert.equal(await page.locator('#aws-new').getAttribute('aria-pressed'),'true');
  assert.equal(await page.locator('#aws-new-guidance').isVisible(),true);
  assert.match(await page.locator('#aws-account-link').getAttribute('href'),/signin\.aws\.amazon\.com/);
  await page.locator('#prepare-install').click();assert.equal(await page.locator('#install-step-0').isVisible(),true);
  await page.screenshot({path:root+'/var/installer-preview/'+engine.name()+'-prepare.png',fullPage:true});
  for(const id of ['ready-aws','ready-toss','ready-telegram','ready-cost'])await page.locator('#'+id).check();
  await page.locator('#prepare-install').click();assert.equal(await page.locator('#install-step-0').isVisible(),true);
  assert.match(await page.locator('#prepare-notice').textContent(),/Spend limit/);
  await page.locator('#aws-plan').selectOption('free');assert.match(await page.locator('#ready-limit-label').textContent(),/Free Tier/);
  await page.locator('#ready-limit').check();await page.locator('#aws-plan').selectOption('paid');assert.equal(await page.locator('#ready-limit').isChecked(),false);
  await page.locator('#aws-existing').click();assert.equal(await page.locator('#aws-new-guidance').isVisible(),false);
  await page.locator('#aws-new').click();await page.locator('#ready-limit').check();
  await page.locator('#prepare-install').click();await page.waitForSelector('#install-step-1:not([hidden])');
  const code=await page.locator('#owner-code').inputValue(),href=await page.locator('#launch-aws').getAttribute('href');
  assert.equal(code.length,64);assert.equal(href.includes(code),false);
  assert.ok(href.includes(crypto.createHash('sha256').update(code).digest('hex')));
  await page.reload();await page.waitForSelector('#install-step-1:not([hidden])');assert.equal(await page.locator('#owner-code').inputValue(),code);assert.equal(await page.locator('#aws-new').getAttribute('aria-pressed'),'true');
  await page.screenshot({path:root+'/var/installer-preview/'+engine.name()+'-aws.png',fullPage:true});
  await page.locator('#aws-complete').click();assert.equal(await page.locator('#install-step-2').isVisible(),true);
  await page.locator('#telegram-complete').click();assert.equal(await page.locator('#install-step-3').isVisible(),true);
  for(const width of [320,768,1200]){await page.setViewportSize({width,height:900});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);}
  await page.locator('#forget-code').click();assert.equal(await page.evaluate(()=>sessionStorage.getItem('l5pha-install-v1')),null);
  assert.deepEqual(errors,[]);console.log(engine.name()+': installer checks, hash-only AWS handoff, reload recovery and layout passed');
 }finally{await browser.close();}}}finally{server.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

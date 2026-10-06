'use strict';
// Headless, keyless interaction checks. Audio and daemon calls are mocked.
process.chdir(require('node:path').resolve(__dirname, '../..'));
require('node:fs').mkdirSync('.context/review', {recursive:true});
let browser;
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 browser=await chromium.launch({...(process.env.ECHOECHO_TEST_BROWSER_EXECUTABLE ? {executablePath:process.env.ECHOECHO_TEST_BROWSER_EXECUTABLE} : {}),headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1200,height:800},reducedMotion:'reduce'});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{window.EventSource=class {constructor(){window.__events=this;this.handlers={};}addEventListener(name,fn){this.handlers[name]=fn;}};});
 let bodies={'older.md':'# Earlier note\n\nChosen by the user.','newer.md':'# Latest note\n\nJust created.'};
 let docCalls=0,failNext=false,race=0;
 await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(url.hostname!=='127.0.0.1')return route.abort();
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync('echoecho_app/viewer/index.html','utf8')});
   if(url.pathname==='/version')return route.fulfill({json:{version:'0.2'}});
   if(url.pathname==='/transcript')return route.fulfill({json:[]});
   if(url.pathname==='/doc'){
    docCalls++;if(failNext){failNext=false;return route.abort();}
    const name=url.searchParams.get('f');let body=bodies[name]||'hello';
    if(race){const n=race++;body=n===1?'Stale response':'Newest response';if(n===1)await new Promise(r=>setTimeout(r,200));}
    return route.fulfill({contentType:'text/plain',body});
   }
   return route.fulfill({status:404,body:''});
 });
 const reload=async files=>{await page.evaluate(files=>window.__events.handlers.reload({data:JSON.stringify({files})}),files);};
 await page.goto('http://127.0.0.1:18765/');
 await reload([{name:'older.md',mtime:1},{name:'newer.md',mtime:2}]);
 await page.getByText('Just created.',{exact:false}).waitFor();
 await page.getByRole('button',{name:'older.md',exact:true}).click();
 await page.getByText('Chosen by the user.',{exact:false}).waitFor();
 const priorCalls=docCalls;
 await reload([{name:'older.md',mtime:1},{name:'newer.md',mtime:3},{name:'another.md',mtime:4}]);
 assert.equal(await page.locator('#filename').textContent(),'older.md');
 assert.equal(await page.getByRole('button',{name:'another.md',exact:true}).count(),1);
 assert.equal(docCalls,priorCalls);
 bodies['older.md']='<script>window.__injected=true</script>\nUpdated chosen file';
 await reload([{name:'older.md',mtime:5},{name:'newer.md',mtime:3}]);
 await page.getByText('Updated chosen file',{exact:false}).waitFor();
 assert.equal(await page.evaluate(()=>window.__injected),undefined);
 assert.equal(await page.locator('#content script').count(),0);
 race=1;
 await reload([{name:'older.md',mtime:6}]);
 await reload([{name:'older.md',mtime:7}]);
 await page.getByText('Newest response',{exact:false}).waitFor();
 await page.waitForTimeout(250);
 assert.match(await page.locator('#content').textContent(),/Newest response/);race=0;
 await reload([]);
 assert.equal(await page.locator('#filebar.show').count(),0);
 assert.equal(await page.locator('#tree button').count(),0);
 failNext=true;
 await reload([{name:'newer.md',mtime:8}]);
 await page.getByText('Could not load the file. Select it again to retry.',{exact:true}).waitFor();
 await page.getByRole('button',{name:'newer.md',exact:true}).click();
 await page.getByText('Just created.',{exact:false}).waitFor();
 assert.deepEqual(errors,[]);
 fs.writeFileSync('.context/review/workspace-result.json',JSON.stringify({passed:true,checks:['selected document stays pinned','new files remain visible','unchanged files are not refetched','offline markdown stays inert','stale responses do not overwrite updates','empty workspace clears selection','failed fetch can be retried'],pageErrors:errors},null,2));
 await browser.close();console.log('Workspace UI: 7 functional checks passed, no page errors.');
})().finally(async()=>{if(browser)await browser.close();}).catch(e=>{console.error(e);process.exit(1);});

const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../../channel/web/static/js/console.js'),'utf8');
const start=source.indexOf('function rebuildCapabilityModelDropdown('),end=source.indexOf('\nfunction ',start+1);
const fn=source.slice(start,end);
test('P06 Web catalog errors remain visible and custom entry remains selectable',async()=>{
 const status={textContent:''},picker={value:'openai'},el={},wrap={classList:{add(){},remove(){}}};let options;
 const root={querySelector:s=>({'#cap-chat-model':el,'#cap-chat-provider':picker,'#cap-chat-catalog-status':status,'#cap-chat-model-custom-wrap':wrap}[s]||null)};
 const context=vm.createContext({document:root,currentLang:'en',modelsState:{providers:[{id:'openai',models:['existing']}],capabilities:{chat:{}}},fetch:async()=>({ok:true,json:async()=>({status:'error',message:'unavailable'})}),getDropdownValue:e=>e.value,t:k=>k,initDropdown:(_e,o)=>options=o});
 vm.runInContext(fn+'\nrebuildCapabilityModelDropdown({id:"chat"},"openai","existing",document);',context);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(status.textContent,'models_catalog_failed');
 assert.ok(options.some(o=>o.value==='existing'));assert.ok(options.some(o=>o.value==='__custom__'));
});

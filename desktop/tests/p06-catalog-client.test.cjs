const test=require('node:test'), assert=require('node:assert/strict'), fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),path=require('node:path');
function client(data){
 const context=vm.createContext({exports:{},require,URL,Headers,Response,Request,FormData,File,Blob,URLSearchParams,ArrayBuffer,Uint8Array,TextEncoder,TextDecoder,atob,crypto,console,window:{electronAPI:{backendRequest:async()=>({status:200,headers:{},bodyBase64:Buffer.from(JSON.stringify(data)).toString('base64')})}}});
 vm.runInContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../src/renderer/src/api/client.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,context);
 return context.exports.default;
}
test('P06 catalog business failure reaches the renderer error path',async()=>{
 await assert.rejects(client({status:'error',message:'catalog unavailable'}).modelCatalog('zhipu'),/catalog unavailable/);
});
test('P06 catalog rejects mismatched provider instead of using another provider list',async()=>{
 await assert.rejects(client({status:'success',provider_id:'openai',models:['different'],discovery:'success'}).modelCatalog('zhipu'));
});
test('P06 valid custom model IDs and explicit discovery failures remain available',async()=>{
 const result=await client({status:'success',provider_id:'zhipu',models:['glm-5.2','custom-model'],discovery:'failed'}).modelCatalog('zhipu');
 assert.equal(result.discovery,'failed');assert.deepEqual(Array.from(result.models),['glm-5.2','custom-model']);
});

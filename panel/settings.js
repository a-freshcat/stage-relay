"use strict";
let settingsDraft=null, editorModels=[], pickedModels=new Set(), editorKey="", editorEpoch=0, editorDirty=false;
let pendingKeys={},editorSavedRef="",editorSavedURL="";
let editorHasCatalog=false;
function settingsMessage(text,error=false){$("settings-message").textContent=text;$("settings-message").className="hint"+(error?" form-error":"");}
function endpointDraft(){
  if($("provider-type").value==="codex")return {codex_provider:$("provider-source").value.trim(),...($("provider-native").checked?{native_auth:true}:{})};
  const url=$("provider-url").value.trim(),auth=$("provider-auth").value;
  return {base_url:url,...(auth==="env"&&$("provider-env").value.trim()?{api_key_env:$("provider-env").value.trim()}:{}),
    ...(auth==="key"&&editorSavedRef&&url.replace(/\/+$/,"")===editorSavedURL?{api_key_ref:editorSavedRef}:{})};
}
function endpointKey(){return JSON.stringify(endpointDraft());}
function roleSelections(){const result={};for(const role of ["planner","worker"])result[role]={provider_id:$(role+"-provider").value,model:$(role+"-model").value};return result;}
function renderRoles(){
  for(const role of ["planner","worker"]){
    const selection=settingsDraft.roles[role], providers=$(role+"-provider"),models=$(role+"-model");
    providers.replaceChildren(Object.assign(node("option","请选择接口"),{value:""}));
    for(const p of settingsDraft.profiles)providers.append(Object.assign(node("option",p.name),{value:p.id}));
    providers.value=selection.provider_id;
    const profile=settingsDraft.profiles.find(p=>p.id===providers.value);
    models.replaceChildren(Object.assign(node("option","请选择已保存的模型"),{value:""}));
    for(const id of profile?.models||[])models.append(Object.assign(node("option",id),{value:id}));
    models.value=selection.model;
    $(role+"-manage").disabled=!profile;
  }
}
function renderProviders(){
  const select=$("edit-provider"),previous=select.value;
  select.replaceChildren(Object.assign(node("option","新增接口…"),{value:""}));
  for(const p of settingsDraft.profiles)select.append(Object.assign(node("option",p.name),{value:p.id}));
  select.value=previous;$("settings-config").textContent=settingsDraft.config_path;
}
function renderModelPicker(){
  const filter=$("provider-filter").value.trim().toLowerCase(),list=$("provider-catalog");
  list.replaceChildren();
  for(const id of editorModels.filter(m=>m.toLowerCase().includes(filter))){
    const label=node("label",undefined,"catalog-option"),box=node("input");box.type="checkbox";box.value=id;box.checked=pickedModels.has(id);
    box.addEventListener("change",()=>{if(box.checked)pickedModels.add(id);else pickedModels.delete(id);editorDirty=true;renderPickedModels();});
    label.append(box,node("span",id));list.append(label);
  }
  if(!editorModels.length)list.append(node("p","先填写接口信息，再点击“读取 API 模型”。","hint"));
  else if(!list.children.length)list.append(node("p","没有匹配的模型。","hint"));
  renderPickedModels();
}
function renderPickedModels(){
  const host=$("provider-picked");host.replaceChildren();
  $("provider-count").textContent=(editorHasCatalog?"本次 API 实际返回 "+editorModels.length+" 个":"尚未读取 API 完整目录")+" · 当前选中 "+pickedModels.size+" 个（不是接口总数）";
  for(const id of pickedModels){
    const chip=node("span",undefined,"model-chip"),remove=node("button","×");remove.type="button";remove.setAttribute("aria-label","删除模型 "+id);
    remove.addEventListener("click",()=>{pickedModels.delete(id);editorDirty=true;renderModelPicker();});
    chip.append(node("span",id),remove);host.append(chip);
  }
}
function providerType(){
  const custom=$("provider-type").value==="custom",auth=$("provider-auth").value;
  $("custom-fields").hidden=!custom;$("codex-fields").hidden=custom;
  $("key-fields").hidden=auth!=="key";$("env-fields").hidden=auth!=="env";
  const saved=!!endpointDraft().api_key_ref;
  const state=settingsDraft?.credential_states?.[$("edit-provider").value],typed=!!$("provider-key").value;
  const badge=$("provider-key-badge");badge.textContent=typed?"已填写 · 尚未保存":saved?(state==="missing"?"密钥不可读取 · 请重新填写":"✓ 已保存 · 本机可读取"):"未保存密钥";
  badge.className="credential-badge "+(saved&&!typed&&state!=="missing"?"credential-saved":"credential-pending");
  $("provider-key").placeholder=saved?"•••••••• 已保存；留空保留，填写新密钥可替换":"粘贴 API Key";
  $("provider-key-state").textContent=saved?(state==="missing"?"配置中有密钥引用，但本机无法读取。请重新填写并保存。":"密钥已安全保存。输入框不回填原密钥；留空再次保存会保留原密钥。"):(typed?"当前填写的密钥只在本次编辑中，点击“保存配置”后才会保存。":"保存配置后，密钥交由 Windows 凭据管理器保存，配置文件仅保存引用。");
  $("provider-clear-key").hidden=!saved;
}
function editProvider(){
  if(!settingsDraft)return;
  const profile=settingsDraft.profiles.find(p=>p.id===$("edit-provider").value),ep=profile?.endpoint||{};
  editorEpoch++;editorDirty=false;editorModels=[];editorHasCatalog=false;pickedModels=new Set(profile?.models||[]);
  $("provider-name").value=profile?.name||"";$("provider-type").value=ep.codex_provider?"codex":"custom";
  $("provider-url").value=ep.base_url||"";$("provider-env").value=ep.api_key_env||"";
  editorSavedRef=ep.api_key_ref||"";editorSavedURL=(ep.base_url||"").replace(/\/+$/,"");
  $("provider-key").value=pendingKeys[profile?.id]||"";
  $("provider-auth").value=ep.api_key_env?"env":(ep.api_key_ref||!profile||pendingKeys[profile?.id]?"key":"none");
  $("provider-source").value=ep.codex_provider||"custom";$("provider-native").checked=!!ep.native_auth;
  $("provider-filter").value="";$("provider-models").value="";$("provider-note").textContent="";
  $("provider-delete").disabled=!profile;$("provider-fetch").disabled=false;
  providerType();editorKey=endpointKey();renderModelPicker();
}
function endpointChanged(){
  const old=JSON.parse(editorKey||"{}"),current=endpointDraft();
  const addressChanged=(old.base_url||"").replace(/\/+$/,"")!==(current.base_url||"").replace(/\/+$/,"")||old.codex_provider!==current.codex_provider;
  if(addressChanged){editorSavedRef="";editorSavedURL="";$("provider-key").value="";pickedModels.clear();}
  providerType();const key=endpointKey();if(key===editorKey)return;
  editorKey=key;editorEpoch++;editorModels=[];editorHasCatalog=false;editorDirty=true;
  $("provider-fetch").disabled=false;$("provider-note").textContent="接口已变更，请重新读取并选择模型。";renderModelPicker();
}
async function fetchEditorModels(){
  const spec=endpointDraft(),key=JSON.stringify(spec),epoch=++editorEpoch;
  $("provider-fetch").disabled=true;$("provider-note").textContent="正在直接读取此 API 的模型目录…";
  try{
    const typed=$("provider-type").value==="custom"&&$("provider-auth").value==="key"?$("provider-key").value.trim():"";
    if($("provider-type").value==="custom"&&$("provider-auth").value==="key"&&!typed&&!spec.api_key_ref)throw new Error("请粘贴 API Key；接口不需要密钥时请选择“此接口无需密钥”。");
    const response=await fetch(new URL("api/models",document.baseURI),{method:"POST",headers:{"Content-Type":"application/json","X-Relay-Panel":"settings"},body:JSON.stringify({endpoint:spec,refresh:true,...(typed?{api_key:typed}:{})})});
    const data=await response.json();if(!response.ok)throw new Error(data.error||"获取模型失败");
    if(epoch!==editorEpoch||endpointKey()!==key)return;
    editorModels=data.models;editorHasCatalog=true;renderModelPicker();
    $("provider-note").textContent="来源："+(spec.base_url?spec.base_url.replace(/\/$/,"")+"/models":"已有供应商 "+spec.codex_provider)+"\nAPI 本次返回 "+editorModels.length+" 个模型，列表已完整展示。目录读取成功；生成能力和账号权限尚未验证。";
  }catch(error){if(epoch===editorEpoch)$("provider-note").textContent=error.message;}
  finally{if(epoch===editorEpoch)$("provider-fetch").disabled=false;}
}
function applyProvider(){
  const name=$("provider-name").value.trim(),models=[...pickedModels];
  if(!name)throw new Error("请填写接口名称。");
  if(!models.length)throw new Error("请先从 API 读取并勾选至少一个模型；不支持目录的接口可明确手动添加。");
  const ep=endpointDraft();if(!(ep.base_url||ep.codex_provider))throw new Error("请填写接口地址或供应商 ID。");
  const typed=$("provider-type").value==="custom"&&$("provider-auth").value==="key"?$("provider-key").value.trim():"";
  if($("provider-type").value==="custom"&&$("provider-auth").value==="key"&&!typed&&!ep.api_key_ref)throw new Error("请填写 API Key，或选择无需密钥。");
  settingsDraft.roles=roleSelections();
  const id=$("edit-provider").value||"local-"+crypto.randomUUID().replaceAll("-","");
  const duplicate=settingsDraft.profiles.find(p=>p.id!==id&&JSON.stringify(p.endpoint)===JSON.stringify(ep));
  if(duplicate)throw new Error("此接口已存在，请从上方选择原接口进行编辑。");
  const profile={id,name,endpoint:ep,models},index=settingsDraft.profiles.findIndex(p=>p.id===id);
  if(index>=0)settingsDraft.profiles[index]=profile;else settingsDraft.profiles.push(profile);
  if(typed)pendingKeys[id]=typed;else delete pendingKeys[id];
  for(const selection of Object.values(settingsDraft.roles))if(selection.provider_id===id&&!models.includes(selection.model))selection.model="";
  renderProviders();$("edit-provider").value=id;$("provider-delete").disabled=false;renderRoles();editorDirty=false;
  $("provider-note").textContent="已加入本次配置，只保留勾选模型。为角色选择后，点击下方“保存配置”写入。";
  settingsMessage("配置有未保存的更改；关闭并重新打开可放弃更改。");
}
$("settings-toggle").addEventListener("click",async()=>{
  $("settings-dialog").showModal();settingsDraft=null;pendingKeys={};$("provider-key").value="";editorEpoch++;
  $("edit-provider").disabled=true;$("provider-fetch").disabled=true;$("provider-delete").disabled=true;
  $("settings-save").disabled=true;$("provider-add").disabled=true;settingsMessage("正在读取本机配置…");
  try{settingsDraft=await api("api/settings");renderProviders();renderRoles();$("edit-provider").disabled=false;$("edit-provider").value=settingsDraft.roles.worker.provider_id;editProvider();
    $("settings-save").disabled=false;$("provider-add").disabled=false;settingsMessage("填写接口 → 读取 API 模型 → 勾选并加入配置 → 分配角色并保存。");
  }catch(error){settingsMessage(error.message,true);}
});
$("settings-close").addEventListener("click",()=>{$("settings-dialog").close();editorEpoch++;});
$("settings-dialog").addEventListener("close",()=>{pendingKeys={};$("provider-key").value="";editorEpoch++;});
$("edit-provider").addEventListener("change",editProvider);
for(const id of ["provider-type","provider-url","provider-env","provider-source","provider-native","provider-auth"])$(id).addEventListener("change",endpointChanged);
$("provider-key").addEventListener("input",()=>{editorDirty=true;editorEpoch++;$("provider-fetch").disabled=false;providerType();});
$("provider-clear-key").addEventListener("click",()=>{$("provider-key").value="";editorSavedRef="";$("provider-auth").value="none";endpointChanged();editorDirty=true;$("provider-note").textContent="保存配置后移除该接口的本机密钥。";});
$("provider-name").addEventListener("input",()=>{editorDirty=true;});
$("provider-filter").addEventListener("input",renderModelPicker);
$("provider-fetch").addEventListener("click",fetchEditorModels);
$("provider-manual-add").addEventListener("click",()=>{
  for(const value of $("provider-models").value.split(/[\n,，]+/).map(s=>s.trim()).filter(Boolean))pickedModels.add(value);
  editorDirty=true;renderModelPicker();$("provider-models").value="";
});
$("provider-add").addEventListener("click",()=>{try{applyProvider();}catch(error){$("provider-note").textContent=error.message;}});
$("provider-delete").addEventListener("click",()=>{
  if(!settingsDraft)return;
  const id=$("edit-provider").value;if(!id)return;
  delete pendingKeys[id];
  settingsDraft.roles=roleSelections();settingsDraft.profiles=settingsDraft.profiles.filter(p=>p.id!==id);
  for(const selection of Object.values(settingsDraft.roles))if(selection.provider_id===id){selection.provider_id="";selection.model="";}
  renderProviders();renderRoles();$("edit-provider").value="";editProvider();
  settingsMessage("接口已从本次配置移除。请为受影响角色重新选择接口和模型后保存；关闭重开可撤销。");
});
for(const role of ["planner","worker"]){
  $(role+"-provider").addEventListener("change",()=>{if(!settingsDraft)return;settingsDraft.roles=roleSelections();settingsDraft.roles[role].model="";renderRoles();});
  $(role+"-manage").addEventListener("click",()=>{$("edit-provider").value=$(role+"-provider").value;editProvider();$("provider-editor").scrollIntoView({block:"start"});});
}
$("settings-save").addEventListener("click",async()=>{
  if(!settingsDraft)return;$("settings-save").disabled=true;
  try{
    if(editorDirty)applyProvider();
    const roles=roleSelections();for(const role of ["planner","worker"]){const profile=settingsDraft.profiles.find(p=>p.id===roles[role].provider_id);if(!profile||!profile.models.includes(roles[role].model))throw new Error("请为"+roleNames[role]+"选择接口及其中已保存的模型。");}
    const response=await fetch(new URL("api/settings",document.baseURI),{method:"POST",headers:{"Content-Type":"application/json","X-Relay-Panel":"settings"},body:JSON.stringify({revision:settingsDraft.revision,profiles:settingsDraft.profiles,roles,api_keys:pendingKeys})});
    const data=await response.json();if(!response.ok)throw new Error(data.error||"保存失败");settingsDraft=data;renderProviders();renderRoles();
    pendingKeys={};$("provider-key").value="";const saved=settingsDraft.profiles.find(p=>p.id===$("edit-provider").value);editorSavedRef=saved?.endpoint.api_key_ref||"";editorSavedURL=(saved?.endpoint.base_url||"").replace(/\/+$/,"");providerType();editorKey=endpointKey();
    settingsMessage(data.credential_warning||"已保存所选模型和接口；API Key 由 Windows 凭据管理器保存。");launchOptions();
  }catch(error){settingsMessage(error.message,true);}
  finally{$("settings-save").disabled=false;}
});

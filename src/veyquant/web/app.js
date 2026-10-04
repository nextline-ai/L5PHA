"use strict";
const byId = id => document.getElementById(id);
const tg = window.Telegram?.WebApp;
let csrf = "", timer = null, reportKey = "", syncing = null;
let lastLoginAttempt = "", suspended = false, connected = false, renewAfter = 0;
const policyFields = ["capital_krw", "max_order_krw", "max_daily_loss_krw"];
const modelRoles = {cheap_model:"cheap", middle_model:"middle", research_model:"research"};
const reasoningLabels = {none:"사용 안 함",minimal:"최소",low:"낮음",medium:"보통",high:"높음",xhigh:"매우 높음",max:"최대"};
const reasoningFields = ["cheap_reasoning","middle_reasoning","research_reasoning"];
let liveSaving = false;
const roleLabels = {cheap:"감시", middle:"정리", research:"의사결정"};
const limitLabels = {capital_krw:"총 운용금액", max_order_krw:"1회 매수 상한", max_daily_loss_krw:"하루 손실 한도"};
let policyRevision = 0, policyDirty = false, latestPolicy = null, policySaving = false;
let modelCatalog = [], catalogKey = "", currentView = "", editorMode = "onboarding", currentStep = 1, editEntryStep = 1;
let rolePromptCatalog = null;
const promptRoles = ["cheap","middle","research"];
let modelPresets = [], limitPresets = [], strategyPresets = [], strategyPreset = "stable", customStrategyDraft = "";
let customModelsMode = false, customLimitsMode = false;
let reportFilter = "all", lastAnalysis = null, reportSymbol = "";
const historyRecords = new Map();
let historyCursor=null, historyStarted=false, historyLoading=false, historyEnd=false, historyGeneration=0;
let marketPage=0, marketRequest=0, marketSearchTimer=null, marketScan=0;
let providerChoice="openai", providerStatus={}, providerSaving=false;
const notice = text => { byId("notice").textContent = text; };
const timeLabel = value => typeof value === "number" ? new Date(value * 1000).toLocaleString("ko-KR") : "아직 없음";
async function api(path, data, timeoutMs=15000) {
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {method: data === undefined ? "GET" : "POST", credentials:"same-origin", cache:"no-store", signal:controller.signal, headers: data === undefined ? {} : {"Content-Type":"application/json", "X-Veyquant-CSRF":csrf}, body:data === undefined ? undefined : JSON.stringify(data)});
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      const error = new Error(detail.error || "request_failed");
      error.status = response.status;
      throw error;
    }
    return await response.json();
  } finally { clearTimeout(deadline); }
}
async function refresh() {
  renderPnl(null,true);
  renderHeroPnl(null,true);
  const data = await api("/v1/status");
  if (suspended) return;
  csrf = data.csrf_token;
  byId("connection").textContent = data.broker_connected ? "수집 중" : "연결 확인 필요";
  byId("snapshot").textContent = `최근 계좌 조회 · ${timeLabel(data.collector?.snapshot_at)}`;
  byId("frames").textContent = typeof data.collector?.frames_received === "number" ? `수신 메시지 ${data.collector.frames_received.toLocaleString("ko-KR")}건` : "연결 정보를 기다리고 있습니다.";
  byId("decision").textContent = data.new_proposals_stopped ? "중단됨" : "관찰 분석";
  byId("stop").disabled = data.new_proposals_stopped;
  byId("resume").hidden = !data.new_proposals_stopped;
  renderModelCatalog(data.model_catalog);
  renderProviders(data.provider_connections);
  renderAnalysis(data.analysis);
  renderPolicy(data.operating_policy);
  renderAccount(data.account, data.readiness);
  renderExecution(data.execution);
  renderUniverseSummary(data.universe);
  renderHero(data);
  if(currentView === "market" && data.universe?.scan_at !== marketScan) {marketScan=data.universe?.scan_at;loadMarket(marketPage);}
  connected = true;
  byId("login").hidden = true;
  byId("dashboard").hidden = false;
  const needsBroker=renderInstallation(data);
  if (!needsBroker && (!currentView || currentView==="broker")) {
    if (data.operating_policy && !data.operating_policy.onboarding_completed) openEditor("onboarding", 1);
    else showView("home");
  }
  byId("app-mode").textContent = data.new_proposals_stopped ? "일시 중단" : data.mode === "live" ? "실거래" : "관찰 분석";
  byId("invitation").value = "";
  if (data.session_expires_at < Date.now() / 1000 + 86400 && Date.now() >= renewAfter) {
    const renewed = await api("/v1/auth/renew", {});
    csrf = renewed.csrf_token;
    renewAfter = Date.now() + 60000;
  }
}
function logout() {
  clearTimeout(timer); timer = null; connected = false; csrf = ""; reportKey = "";
  fullRunCache.clear(); fullRunPanels.clear(); expandedRuns.clear();
  historyRecords.clear(); historyCursor=null; historyStarted=false; historyLoading=false; historyEnd=false; historyGeneration++;
  latestPnl=null;renderPnl(null,true);
  latestMonthlyPnl=null;renderHeroPnl(null,true);
  marketRequest++; clearTimeout(marketSearchTimer); byId("market-stocks").replaceChildren();
  byId("reports").replaceChildren(); byId("dashboard").hidden = true; byId("login").hidden = false;
  byId("onboarding").hidden = true;
  byId("live-banner").hidden = true;
  policyDirty = false; policyRevision = 0; latestPolicy = null;
  currentView = "";
  clearProviderKey();
  clearBrokerKeys();
  for (const field of policyFields) byId(field).value = "";
  byId("bottom-nav").hidden = true;
  byId("policy-notice").textContent = "";
  renderAccount(null, null);
}
function authenticationNotice(error) {
  const binding = ["binding_required", "invalid_invitation"].includes(error.message);
  byId("onboarding").hidden = !binding;
  byId("connect").textContent = binding ? "최초 연결" : "다시 연결";
  byId("welcome").textContent = binding
    ? "최초 연결 코드는 처음 한 번만 사용합니다. 이후에는 Telegram 소유자를 자동으로 확인합니다."
    : "이미 연결한 소유자는 코드 없이 자동 로그인합니다.";
  const messages = {
    binding_required: "처음 연결할 때 받은 코드를 입력해주세요.",
    invalid_invitation: "최초 연결 코드가 올바르지 않거나 만료됐습니다.",
    owner_mismatch: "처음 연결한 소유자의 Telegram 계정으로 열어주세요.",
    already_bound: "이미 소유자가 연결돼 있습니다. 연결 코드는 다시 사용하지 않습니다.",
    telegram_auth_expired: "Telegram 인증 시간이 지났습니다. 미니앱을 완전히 닫고 봇 메뉴에서 다시 열어주세요. 코드는 필요하지 않습니다.",
    authentication_replayed: "Telegram 인증을 새로 받아야 합니다. 미니앱을 완전히 닫고 봇 메뉴에서 다시 열어주세요. 코드는 필요하지 않습니다."
  };
  notice(messages[error.message] || "Telegram 인증을 확인하지 못했습니다. 미니앱을 닫고 @veyquant_bot 메뉴에서 다시 열어주세요.");
}
function sync(force = false, invitation = "") {
  if (syncing) return syncing;
  if (suspended && !force) return Promise.resolve();
  suspended = false;
  clearTimeout(timer);
  byId("connect").disabled = true;
  let retry = false;
  syncing = (async () => {
    try {
      try { await refresh(); }
      catch (error) {
        if (suspended) return;
        if (error.status !== 401) throw error;
        if (!force && tg?.initData && lastLoginAttempt === tg.initData) {
          if (connected) { logout(); authenticationNotice(new Error("telegram_auth_expired")); }
          return;
        }
        logout();
        if (!tg?.initData) {
          notice("Telegram 앱에서 @veyquant_bot의 메뉴 버튼으로 열어주세요."); return;
        }
        lastLoginAttempt = tg.initData;
        try { await api("/v1/auth/login", {init_data:tg.initData}); }
        catch (error) {
          if (error.message !== "binding_required" || !invitation) throw error;
          await api("/v1/auth/bind", {invitation, init_data:tg.initData});
        }
        await refresh();
      }
      if (!suspended) notice("");
    } catch (error) {
      if (suspended) return;
      if (error.status === 401) { logout(); authenticationNotice(error); }
      else {
        retry = true;
        byId("hero-status").textContent="다시 연결 중";
        byId("autonomy-hero").setAttribute("data-signal","attention");
        for(const role of ["cheap","middle","research"]) {byId("hero-"+role).setAttribute("data-active","false");byId("hero-"+role+"-state").textContent="기록 보기 ↗";}
        notice(error.status === 429 ? "요청이 많아 잠시 기다리고 있습니다. 자동으로 다시 확인합니다."
          : "연결이 잠시 끊겼습니다. 로그인 상태를 유지한 채 다시 연결하고 있습니다.");
      }
    } finally {
      syncing = null;
      byId("connect").disabled = false;
      if (!suspended && (connected || retry) && document.visibilityState !== "hidden") {
        timer = setTimeout(() => sync(), 5000);
      }
    }
  })();
  return syncing;
}
byId("connect").addEventListener("click", () => sync(true, byId("invitation").value.trim()));
document.addEventListener("visibilitychange", () => {
  byId("autonomy-hero").setAttribute("data-paused",String(document.visibilityState!=="visible"));
  if (document.visibilityState === "hidden") { clearTimeout(timer); clearProviderKey(); clearBrokerKeys(); }
  else sync();
});
window.addEventListener("online", () => sync());
window.addEventListener("pageshow", () => sync());
byId("stop").addEventListener("click", async () => {
  try { await api("/v1/control/stop", {}); await sync(); notice("신규 판단과 주문을 멈췄습니다. AI 미체결 주문은 취소 결과를 확인합니다."); }
  catch { notice("중단 상태를 확인하지 못했습니다. AWS 비상 관리 경로에서 확인해주세요."); }
});
byId("revoke").addEventListener("click", async () => {
  suspended = true;
  clearTimeout(timer);
  try { await api("/v1/auth/revoke", {}); logout(); notice("모든 세션을 종료했습니다. 다시 연결하려면 미니앱을 닫았다 열어주세요. 코드는 필요하지 않습니다."); }
  catch { suspended = false; notice("세션 종료를 확인하지 못했습니다. 다시 시도해주세요."); }
});
byId("resume").addEventListener("click", async () => {
  try { await api("/v1/control/resume", {}); await sync(); notice("운용을 재개했습니다. 실거래 선택과 한도를 적용합니다."); }
  catch { notice("재개 상태를 확인하지 못했습니다."); }
});
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  const classes = className ? className.split(" ") : [];
  if (tag === "button") classes.push("button", classes.includes("secondary") || classes.includes("quiet-button") ? "button--secondary" : "button--primary");
  if (tag === "input") classes.push("input");
  if (classes.includes("pill")) classes.push("chip", "chip--soft");
  if (classes.length) element.className = classes.join(" ");
  return element;
}
function renderAnalysis(data) {
  lastAnalysis = data;
  if (data?.protocol === "decision-v2") { renderDecisionPipeline(data); return; }
  const states = {unavailable:"준비 중",paused:"일시 중단",analyzing:"분석 중",budget_reached:"오늘 한도 도달",observing:"관찰 중",waiting_for_market:"새 시세 대기",model_setup_required:"모델 연결 필요"};
  byId("analysis-state").textContent = states[data?.state] || "준비 중";
  byId("analysis-budget").textContent = data ? `오늘 분석 ${data.daily_jobs} / ${data.daily_limit}건 · UTC 기준, 실패·예약 포함` : "분석 환경을 준비하고 있습니다.";
  byId("home-analysis").textContent = data?.state === "model_setup_required" ? "선택한 AI의 연결을 기다리고 있습니다. 내 설정에서 연결 상태를 확인해주세요." : data?.reports?.length ? "새로운 관찰 기록이 있습니다. 역할별 판단을 확인해보세요." : "국내 시장의 분석 후보를 관찰하고 있습니다. 새 분석이 완료되면 기록에 표시됩니다.";
  const reports = (data?.reports || []).filter(r=>!reportSymbol || ((r.name||"")+r.symbol).toLowerCase().includes(reportSymbol));
  const key = JSON.stringify([reportFilter,reportSymbol,reports]);
  if (key === reportKey) return;
  reportKey = key;
  for (const role of ["all",...Object.keys(roleLabels)]) byId(`filter-${role}`).setAttribute?.("aria-pressed",String(reportFilter===role));
  byId("analysis-role-help").textContent = reportFilter === "all" ? "세 역할의 판단을 순서대로 확인합니다. 앞 단계에서 보류하면 다음 단계는 호출하지 않습니다." : {cheap:"새 시세에서 더 살펴볼 사건인지 먼저 판단합니다.",middle:"빠른 확인의 판단을 검토하고 추가 조사할 쟁점을 정리합니다.",research:"근거를 종합하고 반대 근거와 불확실성을 함께 제시합니다."}[reportFilter];
  const root = byId("reports"); root.replaceChildren();
  if (!reports.length) { const empty = node("div",undefined,"empty-state"); empty.append(node("div","― · ―","empty-symbol"),node("h2",reportSymbol ? "표시된 기록에 이 종목이 없습니다" : "첫 관찰을 기다리고 있어요"),node("p",reportSymbol ? "검색어를 지우면 최근 분석 기록을 모두 볼 수 있습니다." : "새로운 시세가 들어오면 AI가 살펴봅니다. 분석이 완료되면 각 역할의 판단이 여기에 표시됩니다.")); root.append(empty); return; }
  const actions = {buy:"매수 제안",sell:"매도 제안",hold:"판단 보류",escalate:"다음 단계로 전달",read_evidence:"추가 자료 요청",watch:"추가 관찰",insufficient_evidence:"근거 부족",no_action:"관찰 유지"};
  for (const [index, report] of reports.entries()) {
    const item = node("details", undefined, "report"); item.open = index === 0;
    const title = node("summary"); title.append(node("strong", `${report.name} · ${report.symbol}`), node("span", timeLabel(report.created_at), "fine")); item.append(title);
    item.append(node("p", `관찰 가격 ${report.quote.price} ${report.quote.currency} · ${timeLabel(report.quote.as_of)}`, "fine"));
    if (report.strategy_preset) item.append(node("p", `적용 전략 · ${strategyName(report.strategy_preset)} · 설정 버전 ${report.settings_revision}`, "fine"));
    if (reportFilter === "all") item.append(node("p", report.summary));
    for (const [role,label] of Object.entries(roleLabels)) {
      if (reportFilter !== "all" && reportFilter !== role) continue;
      const stages = (report.stages || []).filter(stage=>stage.role===role);
      const panel = node("section",undefined,stages.length ? "judgment" : "judgment not-run");
      const heading = node("div",undefined,"row"); heading.append(node("strong",label,"stage-label"),node("span",stages.length ? "" : "미실행","stage-action")); panel.append(heading);
      const selected = stages[0]?.model || report.selected_models?.[role];
      if (selected) panel.append(node("p",modelName(selected) + (stages[0]?.reasoning ? ` · 추론 ${reasoningLabels[stages[0].reasoning]}` : ""),"stage-model"));
      if (!stages.length) panel.append(node("p",report.outcome === "error" ? "앞 단계의 응답을 확인하지 못해 이 단계는 호출하지 않았습니다." : "앞 단계에서 종료되어 이 단계는 호출하지 않았습니다.","muted"));
      for (const stage of stages) {
        const decision = stage.decision;
        if (!decision) { panel.append(node("p",stage.status === "uncertain" ? "호출 결과를 확인하지 못했습니다. 판단을 만들어 표시하지 않습니다." : stage.decision_state === "unverified" ? "응답을 검증하지 못해 판단을 표시하지 않습니다." : "이전 기록에는 이 단계의 판단 요약이 저장되지 않았습니다.","muted")); continue; }
        panel.append(node("p",actions[decision.action] || "판단 기록","stage-action"),node("p",decision.summary));
        if (decision.counterargument) panel.append(node("h3","반대 근거"),node("p",decision.counterargument,"muted"));
        if (decision.uncertainty) panel.append(node("h3","확인하지 못한 것"),node("p",decision.uncertainty,"muted"));
      }
      item.append(panel);
    }
    if (reportFilter === "all" && !(report.stages || []).some(s=>s.decision)) {
      item.append(node("h3","기존 종합 기록의 반대 근거"),node("p",report.counterargument,"muted"),node("h3","기존 종합 기록의 불확실성"),node("p",report.uncertainty,"muted"));
    }
    item.append(node("p", report.proposal ? "매매 제안 · 실거래가 활성화되어 있고 계좌·시세·한도 검사를 통과한 경우에만 주문합니다. 체결 결과는 홈의 주문내역에서 확인하세요." : "판단 보류 · 이번 보고서에 따른 주문은 없습니다.", "risk-note"));
    const used = (report.stages || []).reduce((sum,r) => sum + (r.input_tokens || 0) + (r.output_tokens || 0), 0);
    const details = node("details"), summary = node("summary","근거와 사용량"); details.append(summary,node("p", `모델 호출 ${report.stages.length}회 · 확인된 사용량 ${used.toLocaleString("ko-KR")} tokens`, "fine"));
    for (const evidence of report.evidence) details.append(node("p", `${evidence.source} · ${timeLabel(evidence.as_of)}${evidence.status === "missing" ? " · 자료 없음" : ""}`, "source"));
    item.append(details); root.append(item);
  }
}
function normalizedWon(value) { return String(value).replace(/,/g, "").trim(); }
function won(value) {
  return typeof value === "string" && /^\d+(\.\d+)?$/.test(value)
    ? `${Number(value).toLocaleString("ko-KR", {maximumFractionDigits:0})}원` : "설정 전";
}
function friendlyWon(value) {
  const text = normalizedWon(value);
  if (!/^[1-9][0-9]{0,11}$/.test(text)) return "";
  let n = Number(text); const parts = [];
  for (const [unit,label] of [[100000000,"억"],[10000,"만"]]) {
    const count = Math.floor(n / unit); if (count) parts.push(`${count.toLocaleString("ko-KR")}${label}`); n %= unit;
  }
  if (n) parts.push(n.toLocaleString("ko-KR"));
  return `${parts.join(" ")} 원`;
}
function previewPolicy() {
  for (const [field,help] of Object.entries({capital_krw:"capital-help", max_order_krw:"order-help", max_daily_loss_krw:"loss-help"})) byId(help).textContent = friendlyWon(byId(field).value);
  for (const [field,role] of Object.entries(modelRoles)) {
    const item = modelCatalog.find(m => m.id === byId(field).value);
    byId(`${role}-model-help`).textContent = item?.description || "모델을 선택해주세요.";
  }
  updatePresetSelection();
}
function renderReasoning(field, requested) {
  const model=modelCatalog.find(m=>m.id===byId(field).value), select=byId(modelRoles[field]+"_reasoning");
  select.replaceChildren();
  for(const level of model?.reasoning_options || []) {const option=node("option",reasoningLabels[level]);option.value=level;select.append(option);}
  select.value = model?.reasoning_options?.includes(requested) ? requested : ({cheap:"low",middle:"medium",research:"high"}[modelRoles[field]]) || model?.default_reasoning || "";
}
for(const field of reasoningFields) byId(field).addEventListener("change",()=>{customModelsMode=true;edited();});
byId("live_requested").addEventListener("change",edited);
function renderLive() {
  const policy=latestPolicy, visible=!!policy && currentView!=="setup";
  const active=policy?.live_enabled===true, pending=policy?.live_requested===true && !active;
  byId("live-banner").hidden=!visible || policy?.live_requested===true;
  byId("live-banner-title").textContent=pending ? "실거래 조건을 확인하고 있습니다" : "실거래가 비활성화되어 있습니다";
  byId("live-banner-copy").textContent=pending ? policy.live_message : "AI 관찰은 계속됩니다. 실거래 준비 상태를 확인할 수 있습니다.";
  byId("enable-live").hidden=pending;
  byId("live-bottom").hidden=!visible || !(active || pending);
  byId("disable-live").textContent="실거래 비활성화";
  byId("settings-live-state").textContent=active ? "활성" : pending ? "준비 대기" : "비활성";
  byId("settings-live-message").textContent=policy?.live_message || "";
}
async function changeLive(enabled) {
  if(liveSaving || !latestPolicy) return;
  if(!latestPolicy.onboarding_completed) {openEditor("onboarding",1);return;}
  liveSaving=true; byId("enable-live").disabled=true; byId("disable-live").disabled=true;
  try {
    const result=await api("/v1/live-preference",{enabled:String(enabled),expected_revision:String(latestPolicy.revision)});
    if(suspended || !connected) return;
    renderPolicy(result.operating_policy);
    notice(enabled ? result.operating_policy.live_message : "실거래를 비활성화했습니다. 관찰 분석은 계속됩니다.");
  } catch(error) { await sync(); notice(error.status===409 ? "다른 기기에서 설정이 변경됐습니다. 현재 상태를 확인하고 다시 선택해주세요." : "변경 결과를 확인하지 못했습니다. 연결 후 표시되는 실거래 상태를 확인해주세요."); }
  finally {liveSaving=false;byId("enable-live").disabled=false;byId("disable-live").disabled=false;}
}
byId("enable-live").addEventListener("click",()=>changeLive(true));
byId("disable-live").addEventListener("click",()=>changeLive(false));
function modelName(id) { return modelCatalog.find(m => m.id === id)?.name || id || "선택 전"; }
function renderPairs(id, pairs) {
  const root = byId(id); root.replaceChildren();
  for (const [label,value] of pairs) { const row = node("div"); row.append(node("dt",label),node("dd",value)); root.append(row); }
}
function renderModelCatalog(catalog) {
  if (!catalog?.models) return;
  const key = JSON.stringify(catalog);
  if (key === catalogKey) return;
  catalogKey = key; modelCatalog = catalog.models;
  rolePromptCatalog = catalog.role_prompts || null;
  byId("role-prompts-editor").hidden = !rolePromptCatalog;
  byId("edit-role-prompts").hidden = !rolePromptCatalog;
  for(const role of promptRoles) byId(role+"-harness").textContent = rolePromptCatalog?.harnesses?.[role] || "";
  modelPresets = catalog.presets || []; limitPresets = catalog.limit_presets || []; strategyPresets = catalog.strategy_presets || [];
  buildPresetCards();
  for (const field of Object.keys(modelRoles)) {
    const select = byId(field), previous = select.value; select.replaceChildren();
    for (const model of modelCatalog) { const option = node("option",model.name + (model.ready === false ? " · 연결 필요" : "")); option.value = model.id; select.append(option); }
    if (modelCatalog.some(m => m.id === previous)) select.value = previous;
  }
}
function renderPolicy(policy, force = false) {
  if (!policy) return;
  latestPolicy = policy; renderLive();
  byId("settings-model-connection").textContent = policy.model_connection?.message || "";
  byId("strategy-summary-name").textContent = strategyName(policy.strategy?.preset || "stable") + (policy.strategy_configured === false ? " · 기본값" : "");
  byId("strategy-summary-prompt").textContent = policy.strategy?.prompt || "";
  byId("finish-setup").hidden = policy.onboarding_completed === true;
  for (const [id,field] of Object.entries({"home-capital":"capital_krw","home-order-limit":"max_order_krw","home-loss-limit":"max_daily_loss_krw"})) byId(id).textContent = won(policy.limits?.[field]);
  renderPairs("model-summary", Object.entries(roleLabels).map(([role,label])=>[label,modelName(policy.models?.[role]) + (policy.reasoning?.[role] ? ` · 추론 ${reasoningLabels[policy.reasoning[role]]}` : "")]));
  renderPairs("limit-summary", policyFields.map(field=>[limitLabels[field],won(policy.limits?.[field])]));
  byId("settings-saved-at").textContent = policy.configured ? `${timeLabel(policy.updated_at)} 저장` : "아직 운용 한도를 정하지 않았습니다.";
  byId("policy-state").textContent = policyDirty && !force ? "아직 저장하지 않은 변경이 있습니다." : "저장된 설정은 언제든 변경할 수 있습니다.";
  if (policyDirty && !force) {
    if (policy.revision !== policyRevision) {
      byId("policy-notice").textContent = "다른 기기에서 설정이 변경됐습니다. 최신 설정을 불러온 뒤 다시 수정해주세요.";
      byId("reload-policy").hidden = false;
    }
    return;
  }
  policyRevision = policy.revision; policyDirty = false; customModelsMode=false; customLimitsMode=false;
  for (const field of policyFields) byId(field).value = policy.limits?.[field] ? Number(policy.limits[field]).toLocaleString("ko-KR") : "";
  for (const [field,role] of Object.entries(modelRoles)) byId(field).value = policy.models?.[role] || "";
  for (const [field,role] of Object.entries(modelRoles)) renderReasoning(field,policy.reasoning?.[role]);
  byId("live_requested").value = String(policy.live_requested === true);
  strategyPreset = policy.strategy?.preset || "stable";
  byId("strategy_prompt").value = policy.strategy?.prompt || strategyPresets.find(p=>p.id===strategyPreset)?.prompt || "";
  customStrategyDraft = strategyPreset === "custom" ? byId("strategy_prompt").value : "";
  for(const role of promptRoles) byId(role+"_prompt").value = policy.prompts?.[role] || rolePromptCatalog?.defaults?.[role] || "";
  renderPromptCounts();
  byId("reload-policy").hidden = true; previewPolicy();
}
function showView(view) {
  if(view === "market") loadMarket(0);
  if (currentView !== view) notice("");
  if(currentView !== view) {clearProviderKey();byId("optional-panel").hidden=true;}
  currentView = view; renderLive();
  const providerSlot=byId(view === "settings" ? "settings-provider-slot" : "setup-provider-slot");
  providerSlot.append(byId("provider-panel"));
  for (const name of ["home","market","reports","settings","setup"]) byId(`${name}-screen`).hidden = name !== view;
  byId("bottom-nav").hidden = view === "setup";
  for (const name of ["home","market","reports","settings"]) {
    if (name === view) byId(`nav-${name}`).setAttribute?.("aria-current","page");
    else byId(`nav-${name}`).removeAttribute?.("aria-current");
  }
  if (view === "setup") tg?.BackButton?.show(); else tg?.BackButton?.hide();
  window.scrollTo?.({top:0});
}
function openEditor(mode, step) {
  editorMode = mode; editEntryStep = step;
  if(step===1) byId("custom-models").open = customModelsMode || !modelPresets.some(p=>Object.entries(modelRoles).every(([field,role])=>byId(field).value===p.models[role] && byId(role+"_reasoning").value===p.reasoning?.[role]));
  byId("setup-close").textContent = mode === "onboarding" ? "나중에" : "닫기";
  byId("save-settings").textContent = mode === "onboarding" ? "설정 완료하고 시작하기" : "변경 내용 저장";
  showView("setup"); showStep(step);
}
function showStep(step) {
  currentStep = step;
  for (const n of [1,2,3,4]) {
    byId(`step-${n}`).hidden = n !== step;
    byId(`progress-${n}`).className = n <= step ? "active" : "";
  }
  byId("setup-progress").textContent = editorMode === "onboarding" ? `시작하기 · ${step} / 4` : "내 설정 변경";
  byId("setup-next").hidden = step === 4;
  byId("save-settings").hidden = step !== 4;
  byId("setup-next").textContent = editorMode === "onboarding" ? ({1:"다음 · 운용 한도",2:"다음 · 투자 전략",3:"다음 · 설정 확인"}[step] || "다음") : "다음 · 설정 확인";
  if (step === 4) {
    renderPairs("review-models",Object.entries(modelRoles).map(([field,role])=>[roleLabels[role],modelName(byId(field).value) + ` · 추론 ${reasoningLabels[byId(role+"_reasoning").value] || "선택 전"}`]));
    renderPairs("review-limits",policyFields.map(field=>[limitLabels[field],won(normalizedWon(byId(field).value))]));
    byId("review-strategy-name").textContent = strategyName(strategyPreset);
    byId("review-strategy-prompt").textContent = byId("strategy_prompt").value.trim();
    const review=byId("review-role-prompts");review.replaceChildren();review.hidden=!rolePromptCatalog;
    if(rolePromptCatalog) for(const role of promptRoles) appendStructured(review,roleLabels[role]+" · 시스템 프롬프트",byId(role+"_prompt").value.trim());
    byId("review-connection-note").textContent = connectionNote();
  }
  window.scrollTo?.({top:0});
  byId({1:"model-heading",2:"limits-heading",3:"strategy-heading",4:"review-heading"}[step]).focus?.();
}
function clearFieldErrors() {
  for (const field of [...policyFields,...Object.keys(modelRoles),"strategy_prompt",...promptRoles.map(r=>r+"_prompt")]) byId(field).removeAttribute?.("aria-invalid");
  byId("policy-notice").textContent = "";
}
function fieldError(field, text) {
  byId(field).setAttribute?.("aria-invalid","true");
  byId("policy-notice").textContent = text;
  byId(field).focus?.(); return false;
}
function validateModels() {
  for (const field of Object.keys(modelRoles)) if (!modelCatalog.some(m=>m.id === byId(field).value)) return fieldError(field,"각 역할에 사용할 AI 모델을 선택해주세요.");
  for (const [field,role] of Object.entries(modelRoles)) {
    const model=modelCatalog.find(m=>m.id===byId(field).value);
    if(!model.reasoning_options?.includes(byId(role+"_reasoning").value)) return fieldError(role+"_reasoning","각 모델의 추론 수준을 선택해주세요.");
  }
  return true;
}
function validateLimits() {
  for (const field of policyFields) if (!/^[1-9][0-9]{0,11}$/.test(normalizedWon(byId(field).value))) return fieldError(field,`${limitLabels[field]}을 1원 이상의 정수로 입력해주세요.`);
  const capital = Number(normalizedWon(byId("capital_krw").value));
  for (const field of ["max_order_krw","max_daily_loss_krw"]) if (Number(normalizedWon(byId(field).value)) > capital) return fieldError(field,`${limitLabels[field]}은 총 운용금액 이하여야 합니다.`);
  return true;
}
function validateStrategy() {
  if(rolePromptCatalog) for(const role of promptRoles) {
    const value=byId(role+"_prompt").value.trim();
    if(!value || Array.from(value).length>rolePromptCatalog.max_chars || /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(value)) {
      byId("role-prompts-editor").open=true;
      return fieldError(role+"_prompt",`${roleLabels[role]} 프롬프트를 1~${rolePromptCatalog.max_chars.toLocaleString("ko-KR")}자로 입력해주세요.`);
    }
  }
  const text = byId("strategy_prompt").value.trim();
  if (!text || Array.from(text).length > 3000 || /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(text)) return fieldError("strategy_prompt","투자 전략을 1~3,000자로 입력해주세요.");
  return true;
}
function edited() {
  policyDirty = true; byId("policy-state").textContent = "아직 저장하지 않은 변경이 있습니다.";
  clearFieldErrors(); previewPolicy();
}
for (const field of policyFields) {
  byId(field).addEventListener("input",()=>{customLimitsMode=true;edited();});
  byId(field).addEventListener("blur",()=>{
    const value = normalizedWon(byId(field).value);
    if (/^[1-9][0-9]{0,11}$/.test(value)) byId(field).value = Number(value).toLocaleString("ko-KR");
  });
}
for (const field of Object.keys(modelRoles)) byId(field).addEventListener("change",()=>{customModelsMode=true;renderReasoning(field);edited();});
for (const view of ["home","market","reports","settings"]) byId(`nav-${view}`).addEventListener("click",()=>showView(view));
byId("finish-setup").addEventListener("click",()=>openEditor("onboarding",1));
byId("home-settings").addEventListener("click",()=>showView("settings"));
byId("view-reports").addEventListener("click",()=>showView("reports"));
byId("edit-models").addEventListener("click",()=>openEditor(latestPolicy?.onboarding_completed ? "settings" : "onboarding",1));
byId("edit-limits").addEventListener("click",()=>openEditor(latestPolicy?.onboarding_completed ? "settings" : "onboarding",2));
byId("review-edit-models").addEventListener("click",()=>showStep(1));
byId("review-edit-limits").addEventListener("click",()=>showStep(2));
byId("edit-strategy").addEventListener("click",()=>openEditor(latestPolicy?.onboarding_completed ? "settings" : "onboarding",3));
byId("review-edit-strategy").addEventListener("click",()=>showStep(3));
byId("setup-close").addEventListener("click",()=>{if(!policySaving) showView(editorMode === "onboarding" ? "home" : "settings");});
function backFromEditor() {
  if (policySaving) return;
  if (currentStep === 4) showStep(editorMode === "settings" ? editEntryStep : 3);
  else if (currentStep > 1 && editorMode === "onboarding") showStep(currentStep-1);
  else showView(editorMode === "onboarding" ? "home" : "settings");
}
byId("setup-back").addEventListener("click",backFromEditor);
tg?.BackButton?.onClick(backFromEditor);
byId("setup-next").addEventListener("click",()=>{
  clearFieldErrors();
  if (currentStep === 1) {
    if (!validateModels()) return;
    if (editorMode === "onboarding") return showStep(2);
  }
  if (!validateLimits()) {if(currentStep!==2) showStep(2);return;}
  if (currentStep === 2 && editorMode === "onboarding") return showStep(3);
  if (!validateStrategy()) {showStep(3);return;}
  showStep(4);
});
byId("reload-policy").addEventListener("click", async()=>{
  if (policySaving) return;
  await sync();
  if (latestPolicy) renderPolicy(latestPolicy,true);
  byId("policy-notice").textContent = "최신 설정을 불러왔습니다. 변경할 내용을 다시 확인해주세요.";
  showStep(editEntryStep);
});
byId("save-settings").addEventListener("click", async()=>{
  if (policySaving || !latestPolicy) return;
  clearFieldErrors();
  if (!validateModels()) {showStep(1);return;}
  if (!validateLimits()) {showStep(2);return;}
  if (!validateStrategy()) {showStep(3);return;}
  const data = {expected_revision:String(policyRevision),strategy_preset:strategyPreset,strategy_prompt:byId("strategy_prompt").value.trim()};
  if(rolePromptCatalog) for(const role of promptRoles) data[role+"_prompt"]=byId(role+"_prompt").value.trim();
  for (const field of policyFields) data[field] = normalizedWon(byId(field).value);
  for (const field of [...Object.keys(modelRoles),...reasoningFields,"live_requested"]) data[field] = byId(field).value;
  const disabled = [...policyFields,...Object.keys(modelRoles),...reasoningFields,"live_requested","save-settings","setup-back","setup-close","setup-next","review-edit-models","review-edit-limits","review-edit-strategy","strategy_prompt","customize-strategy",...promptRoles.flatMap(r=>[r+"_prompt","reset-"+r+"-prompt"]),...presetButtonIds()];
  policySaving = true; policyDirty = true;
  for (const id of disabled) byId(id).disabled = true;
  byId("save-settings").textContent = "저장 중…";
  try {
    const result = await api("/v1/settings",data);
    if (suspended || !connected) return;
    renderPolicy(result.operating_policy,true);
    showView(editorMode === "onboarding" ? "home" : "settings");
    notice(result.operating_policy.model_connection?.ready === false ? "설정은 저장했습니다. 선택한 AI가 연결되면 분석을 시작합니다." : "설정을 저장했습니다. 다음 분석부터 적용됩니다.");
  } catch (error) {
    if (error.status === 409) {
      byId("policy-notice").textContent = "다른 기기에서 설정이 변경됐습니다. 최신 설정을 불러온 뒤 다시 수정해주세요.";
      byId("reload-policy").hidden = false; await sync();
    } else {
      byId("policy-notice").textContent = "저장 결과를 확인하지 못했습니다. 입력값을 유지합니다. 연결이 복구되면 다시 확인해주세요.";
    }
  } finally {
    policySaving = false;
    for (const id of disabled) byId(id).disabled = false;
    byId("save-settings").textContent = editorMode === "onboarding" ? "설정 완료하고 시작하기" : "변경 내용 저장";
  }
});
function renderAccount(account, readiness) {
  const fresh = account?.state === "fresh";
  byId("account-state").textContent = fresh ? "조회 완료" : "조회 대기";
  byId("account-cash").textContent = fresh ? won(account.cash_buying_power_krw) : "—";
  byId("account-value").textContent = fresh ? won(account.domestic_market_value_krw) : "—";
  byId("account-orders").textContent = fresh ? `미체결 ${account.open_orders}건 · 조건주문 ${account.conditional_orders}건` : "최신 계좌 조회를 기다리고 있습니다.";
  byId("account-date").textContent = fresh ? `${timeLabel(account.snapshot_at)} 기준` : "연결 상태를 확인하고 있습니다.";
  const list = byId("readiness"); list.replaceChildren();
  for (const check of readiness?.checks || []) list.append(node("li",check.label,check.passed ? "done" : "pending"));
}
function strategyName(id) { return id === "custom" ? "커스텀" : strategyPresets.find(p=>p.id===id)?.name || {stable:"안정",active:"적극",aggressive:"공격"}[id] || "기록 없음"; }
function presetButtonIds() { return [...modelPresets.map(p=>`model-preset-${p.id}`),"model-preset-custom",...limitPresets.map(p=>`limit-preset-${p.id}`),"limit-preset-custom",...strategyPresets.map(p=>`strategy-preset-${p.id}`),"strategy-preset-custom"]; }
function presetCard(root, id, title, description, state, callback) {
  const button = node("button",undefined,"preset-card"); button.id = id; button.type = "button";
  button.setAttribute?.("aria-pressed","false");
  button.append(node("strong",title),node("small",description));
  if (state) button.append(node("span",state,"preset-state"));
  button.addEventListener("click",()=>{if(!policySaving) callback();}); root.append(button);
}
function buildPresetCards() {
  const mr=byId("model-presets"),lr=byId("limit-presets"),sr=byId("strategy-presets");
  mr.replaceChildren(); lr.replaceChildren(); sr.replaceChildren();
  for (const p of modelPresets) presetCard(mr,`model-preset-${p.id}`,p.name,p.description,p.ready ? "연결 가능" : "연결 필요",()=>{
    for (const [field,role] of Object.entries(modelRoles)) byId(field).value=p.models[role];
    for (const [field,role] of Object.entries(modelRoles)) renderReasoning(field,p.reasoning?.[role]);
    customModelsMode=false; byId("custom-models").open=false;
    if(p.id === "chatgpt" || p.id === "gemini") chooseProvider(p.id === "chatgpt" ? "openai" : "gemini");
    edited();
  });
  presetCard(mr,"model-preset-custom","커스텀","세 역할을 직접 조합",null,()=>{customModelsMode=true; byId("custom-models").open=true;edited();byId("cheap_model").focus?.();});
  for (const p of limitPresets) presetCard(lr,`limit-preset-${p.id}`,p.name,p.description,null,()=>{
    for (const field of policyFields) byId(field).value=Number(p.limits[field]).toLocaleString("ko-KR");
    customLimitsMode=false; edited();
  });
  presetCard(lr,"limit-preset-custom","직접 입력","세 금액을 자유롭게 설정",null,()=>{customLimitsMode=true;updatePresetSelection();byId("capital_krw").focus?.();});
  for (const p of strategyPresets) presetCard(sr,`strategy-preset-${p.id}`,p.name,p.description,null,()=>{
    if(strategyPreset==="custom") customStrategyDraft=byId("strategy_prompt").value;
    strategyPreset=p.id;byId("strategy_prompt").value=p.prompt;edited();
  });
  presetCard(sr,"strategy-preset-custom","커스텀","나만의 판단 기준 작성",null,()=>{
    strategyPreset="custom";byId("strategy_prompt").value=customStrategyDraft;edited();byId("strategy_prompt").focus?.();
  });
}
function connectionNote() {
  const missing = [...new Set(Object.keys(modelRoles).map(field=>byId(field).value))].filter(id=>modelCatalog.find(m=>m.id===id)?.ready===false);
  const parts=[];
  if(missing.some(id=>id.startsWith("gpt-"))) parts.push("OpenAI API 키와 선택한 모델의 접근 확인이 필요합니다.");
  if(missing.some(id=>id.startsWith("gemini-"))) parts.push("Gemini API 키 연결이 필요합니다.");
  return parts.length ? parts.join(" ")+" 선택은 저장할 수 있으며, 연결 전에는 분석을 기다립니다." : "";
}
function updatePresetSelection() {
  const selectedModel=customModelsMode ? "custom" : modelPresets.find(p=>Object.entries(modelRoles).every(([field,role])=>byId(field).value===p.models[role] && byId(role+"_reasoning").value===(p.reasoning?.[role] || {cheap:"low",middle:"medium",research:"high"}[role])))?.id || "custom";
  const selectedLimit=customLimitsMode ? "custom" : limitPresets.find(p=>policyFields.every(f=>normalizedWon(byId(f).value)===p.limits[f]))?.id || "custom";
  for (const p of [...modelPresets,{id:"custom"}]) byId(`model-preset-${p.id}`).setAttribute?.("aria-pressed",String(p.id===selectedModel));
  for (const p of [...limitPresets,{id:"custom"}]) byId(`limit-preset-${p.id}`).setAttribute?.("aria-pressed",String(p.id===selectedLimit));
  for (const p of [...strategyPresets,{id:"custom"}]) byId(`strategy-preset-${p.id}`).setAttribute?.("aria-pressed",String(p.id===strategyPreset));
  byId("strategy_prompt").readOnly=strategyPreset!=="custom";
  byId("customize-strategy").hidden=strategyPreset==="custom";
  byId("prompt-count").textContent=`${Array.from(byId("strategy_prompt").value).length.toLocaleString("ko-KR")} / 3,000`;
  byId("model-connection-note").textContent=connectionNote();
  byId("setup-provider-slot").hidden = !Object.keys(modelRoles).some(f=>/^(gpt-|gemini-)/.test(byId(f).value));
}
byId("strategy_prompt").addEventListener("input",()=>{strategyPreset="custom";customStrategyDraft=byId("strategy_prompt").value;edited();});
byId("customize-strategy").addEventListener("click",()=>{strategyPreset="custom";customStrategyDraft=byId("strategy_prompt").value;edited();byId("strategy_prompt").focus?.();});
for (const role of ["all",...Object.keys(roleLabels)]) byId(`filter-${role}`).addEventListener("click",()=>{reportFilter=role;renderAnalysis(lastAnalysis);});
function clearProviderKey() { byId("provider-api-key").value=""; byId("optional-api-key").value=""; }
function chooseProvider(provider) {
  if(providerSaving) return;
  clearProviderKey(); providerChoice=provider; byId("provider-notice").textContent=""; renderProviders();
}
function renderProviders(status) {
  if(status) providerStatus=status;
  const saved=providerStatus[providerChoice] || {}, name={openai:"OpenAI",gemini:"Gemini"}[providerChoice];
  byId("provider-openai").setAttribute?.("aria-pressed",String(providerChoice==="openai"));
  byId("provider-gemini").setAttribute?.("aria-pressed",String(providerChoice==="gemini"));
  byId("provider-key-label").textContent=`${name} ${saved.connected ? "새 " : ""}API 키`;
  byId("provider-status").textContent=saved.connected ? `키 저장됨 · 모델 접근 확인 ${timeLabel(saved.verified_at)}` : `${name} API 키를 연결해주세요.`;
  byId("provider-model-status").textContent=saved.connected ? `접근 확인: ${(saved.models || []).map(modelName).join(" · ")}. 실제 생성 가능 여부는 분석 실행 시 확인합니다.` : "";
  renderOptionalConnections();
  if(!providerSaving) byId("connect-provider").textContent=saved.connected ? "새 키 확인하고 교체" : "키 확인하고 연결";
}
byId("provider-openai").addEventListener("click",()=>chooseProvider("openai"));
byId("provider-gemini").addEventListener("click",()=>chooseProvider("gemini"));
byId("connect-provider").addEventListener("click",async()=>{
  if(providerSaving) return;
  let key=byId("provider-api-key").value.trim();
  if(!/^[A-Za-z0-9_-]{20,512}$/.test(key)) { byId("provider-notice").textContent="발급받은 API 키 전체를 확인해주세요."; return; }
  providerSaving=true; clearProviderKey();
  for(const id of ["connect-provider","provider-openai","provider-gemini","provider-api-key"]) byId(id).disabled=true;
  byId("connect-provider").textContent="연결 확인 중…";
  byId("provider-notice").textContent="키와 모델 접근을 확인하고 있습니다. 잠시만 기다려주세요.";
  try {
    const pending=api("/v1/providers/connect",{provider:providerChoice,api_key:key},110000); key="";
    const result=await pending;
    if(!connected || suspended) return;
    renderProviders(result.provider_connections); await sync(); updatePresetSelection();
    byId("provider-notice").textContent="키를 암호화해 저장했습니다. 사용할 모델과 설정을 확인해주세요.";
  } catch(error) {
    const messages={invalid_provider_key:"API 키 형식을 확인해주세요.",provider_connection_busy:"다른 연결 확인이 진행 중입니다. 잠시 후 다시 시도해주세요.",authentication_rate_limited:"연결 시도가 많습니다. 1분 후 다시 시도해주세요."};
    byId("provider-notice").textContent=messages[error.message] || "연결을 확인하지 못했습니다. 키와 API 계정의 모델 접근 권한을 확인해주세요. 기존 연결은 유지됩니다.";
  } finally {
    key=""; clearProviderKey(); providerSaving=false;
    for(const id of ["connect-provider","provider-openai","provider-gemini","provider-api-key"]) byId(id).disabled=false;
    renderProviders();
  }
});
// The brand stays light even when Telegram uses dark mode. Older clients may not
// support chrome colors; a theme failure must never interrupt authentication.
function applyBrandTheme() {
  for (const method of ["setHeaderColor", "setBackgroundColor", "setBottomBarColor"]) {
    try { tg?.[method]?.("#ffffff"); } catch { /* Optional Telegram chrome API. */ }
  }
}
applyBrandTheme();
tg?.onEvent?.("themeChanged", applyBrandTheme);
tg?.ready(); tg?.expand();
tg?.onEvent?.("activated",()=>sync());
sync();

var executionOrderKey, pendingExecutionId;
var latestPnl=null, latestMonthlyPnl=null;
function renderPnl(data,loading=false) {
  const today=new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Seoul",year:"numeric",month:"2-digit",day:"2-digit"}).format(new Date());
  if(latestPnl?.day!==today) latestPnl=null;
  const value=data?.loss?.pnl_krw, day=data?.loss?.day || today;
  const valid=!loading && typeof value==="string" && value.trim()!=="" && Number.isFinite(Number(value)) && day===today;
  if(valid) latestPnl={day,amount:Number(value),value:Number(value).toLocaleString("ko-KR",{maximumFractionDigits:0})+"원",at:data.updated_at || Date.now()/1000};
  byId("execution-pnl").textContent=latestPnl?.value || "—";
  byId("execution-pnl").setAttribute("title",latestPnl ? `마지막 확인 · ${timeLabel(latestPnl.at)}` : "아직 확인된 운용손익이 없습니다.");
  byId("execution-pnl-loading").hidden=valid;

}
function renderExecution(data) {
  const completed=(data?.command_results || []).find(c=>c.id===pendingExecutionId);
  if(completed) {
    const messages={attached:"토스 주문과 연결했습니다.",owner_confirmed_absent:"접수되지 않은 주문으로 기록했습니다.",cancel_requested:"취소를 요청했습니다. 체결·취소 결과를 계속 확인합니다.",not_applicable:"입력한 주문번호와 조건이 맞지 않거나 처리 상태가 바뀌었습니다.",review_required:"체결 내역이 일치하지 않아 주문을 멈췄습니다.",lookup_failed:"토스 주문번호를 조회하지 못했습니다. 번호와 연결 상태를 확인해주세요."};
    byId("execution-result").textContent=messages[completed.result] || "주문 상태를 확인해주세요."; pendingExecutionId=undefined;
  }
  const labels={active:"자동 운용 중",disabled:"관찰 분석",ready:"준비 완료",market_closed:"거래 시간 대기",daily_loss_limit:"오늘 주문 중단",stopped:"일시 중단",order_review:"주문 확인 필요"};
  byId("execution-state").textContent=labels[data?.state] || "확인 중";
  byId("execution-message").textContent=data?.message || "자동 운용 상태를 확인하고 있습니다.";
  renderPnl(data);
  renderHeroPnl(data);
  const positions=data?.managed_positions || [];
  byId("execution-shares").textContent=data?.available ? `${positions.length}종목` : "확인 중";
  const positionRoot=byId("managed-positions"); positionRoot.replaceChildren();
  for(const p of positions) {const row=node("div",undefined,"position-row");row.append(node("span",`${p.name || p.symbol} · ${p.symbol}`),node("strong",`${p.quantity.toLocaleString("ko-KR")}주`));positionRoot.append(row);}
  byId("execution-action").textContent=data?.last_action_message || "";
  const orders=data?.orders || [];
  byId("execution-order-count").textContent=orders.length ? String(orders.length) : "";
  const key=JSON.stringify(orders.map(({updated_at,...o})=>o));
  if(key===executionOrderKey) return;
  executionOrderKey=key;
  const root=byId("execution-orders"); root.replaceChildren();
  if(!orders.length) {root.append(node("p","아직 주문이 없습니다. 관찰 분석 중에는 실제 주문을 보내지 않습니다.","fine"));return;}
  const states={PREPARED:"주문 전 확인",SENDING:"전송 중",UNKNOWN:"접수 결과 확인 필요",ACKNOWLEDGED:"접수 · 체결 대기",PARTIAL:"일부 체결",FILLED:"체결 완료",CANCELED:"취소 완료",REJECTED:"거절",VOID:"주문하지 않음",CANCEL_SENDING:"취소 전송 중",CANCEL_PENDING:"취소 결과 확인 중",CANCEL_UNKNOWN:"취소 결과 확인 필요",REVIEW:"내역 대조 필요"};
  for(const order of orders) {
    const card=node("article",undefined,"order-entry");
    const row=node("div",undefined,"row"); row.append(node("strong",`${order.side==="BUY" ? "매수" : "매도"} ${order.quantity}주`),node("span",states[order.state]||"확인 필요","pill")); card.append(row);
    card.append(node("p",`${order.name || order.symbol} · ${order.symbol} · 지정가 ${won(order.limit_price)} · ${timeLabel(order.created_at)}`,"fine"),node("p",`체결 ${order.filled_quantity} / ${order.quantity}주 · ${won(order.filled_amount)}`,"fine"));
    if(["ACKNOWLEDGED","PARTIAL"].includes(order.state)) {
      const cancel=node("button","남은 수량 취소","secondary"); cancel.addEventListener("click",()=>sendExecutionCommand(order,"cancel",{},cancel)); card.append(cancel);
    }
    if(order.state==="UNKNOWN") {
      const recovery=node("details"), title=node("summary","토스 주문내역과 연결하기"); recovery.append(title,node("p","응답을 받지 못한 주문입니다. 먼저 실거래를 비활성화하고 토스에서 접수·체결 내역을 확인해주세요. 접수되지 않았다는 확인은 주문 시도 후 24시간이 지나야 적용할 수 있습니다.","fine"));
      const id=`broker-${order.id}`, label=node("label","토스 주문번호"); label.htmlFor=id;
      const input=node("input"); input.id=id; input.maxLength=200; input.autocomplete="off"; input.placeholder="일치하는 토스 주문번호";
      const checkbox=node("input"); checkbox.type="checkbox"; checkbox.id=`confirm-${order.id}`;
      const confirm=node("label","토스에서 주문 결과를 직접 확인했습니다."); confirm.htmlFor=checkbox.id;
      const attach=node("button","이 주문번호로 대조","secondary"), absent=node("button","접수된 주문이 없음을 확인","quiet-button");
      const act=(action,button)=>{if(latestPolicy?.live_requested){notice("먼저 화면 아래에서 실거래를 비활성화해주세요.");return;}if(!checkbox.checked){notice("토스 주문내역 확인이 필요합니다.");return;}sendExecutionCommand(order,action,{confirmation:"confirmed_in_toss",...(action==="attach" ? {broker_id:input.value.trim()} : {})},button);};
      attach.addEventListener("click",()=>act("attach",attach)); absent.addEventListener("click",()=>act("confirm_absent",absent));
      recovery.append(label,input,checkbox,confirm,attach,absent); card.append(recovery);
    }
    root.append(card);
  }
}
async function sendExecutionCommand(order,action,extra,button) {
  button.disabled=true;
  byId("execution-result").textContent="요청을 저장하고 있습니다.";
  try {
    const response=await api("/v1/execution-command",{order_id:order.id,action,expected_revision:String(latestPolicy.revision),...extra});
    pendingExecutionId=response.request_id;
    byId("execution-result").textContent="요청을 저장했습니다. 토스 대조 후 주문 상태에 반영됩니다.";
    await sync();
  } catch(error) {notice(error.status===409 ? "현재 설정과 주문 상태가 바뀌었습니다. 새 상태를 확인해주세요." : "요청 결과를 확인하지 못했습니다. 주문 상태를 먼저 확인해주세요."); await sync();}
  finally {button.disabled=false;}
}

function renderUniverseSummary(data) {
  byId("universe-badge").textContent=!data?.available ? "준비 중" : !data.catalogue_fresh ? "목록 확인 중" : data.state==="partial_prices" ? "일부 시세 대기" : data.state==="scan_unavailable" ? "탐색 재연결 중" : "전체 시장 탐색";
  byId("universe-summary").textContent=data?.available ? `국내 ${data.total.toLocaleString("ko-KR")}종목 · 시세 확인 ${data.covered.toLocaleString("ko-KR")}종목 · 실시간 관찰 ${data.watch_count}종목` : "토스의 국내 종목 목록을 불러오고 있습니다.";
  byId("universe-summary-time").textContent=data?.scan_at ? `마지막 전체 탐색 · ${timeLabel(data.scan_at)}` : "";
}
async function loadMarket(page=0) {
  const request=++marketRequest;
  const q=encodeURIComponent(byId("stock-search").value.trim()), market=encodeURIComponent(byId("stock-market").value);
  byId("market-notice").textContent="종목을 불러오고 있습니다.";
  byId("market-prev").disabled=byId("market-next").disabled=true;
  try {
    const data=await api(`/v1/universe?q=${q}&market=${market}&page=${page}`);
    if(request!==marketRequest || suspended) return;
    marketPage=page;
    byId("market-count").textContent=data.available ? `${data.matched.toLocaleString("ko-KR")}종목 · 마지막 탐색 ${timeLabel(data.scan_at)}` : "종목 목록을 준비하고 있습니다.";
    byId("market-notice").textContent=data.available && !data.catalogue_fresh ? "종목 목록을 갱신하고 있습니다. 신규 매수는 최신 목록을 확인한 뒤 진행합니다." : "";
    const root=byId("market-stocks"); root.replaceChildren();
    const reasons={eligible:"분석 대상",suspended:"거래정지",liquidation:"정리매매 · 제외",metadata_missing:"거래 정보 확인 필요",not_active:"상장 상태 확인 필요"};
    for(const stock of data.items || []) {
      const card=node("article",undefined,"stock-entry"), row=node("div",undefined,"row");
      row.append(node("strong",stock.name),node("span",stock.price ? won(stock.price) : "시세 대기","stock-price"));
      card.append(row,node("p",`${stock.symbol} · ${{KOSPI:"코스피",KOSDAQ:"코스닥",KR_ETC:"국내 기타"}[stock.market] || stock.market}${stock.common_share===false ? " · 우선주" : ""}`,"fine"));
      const status=stock.subscription_rejected ? "실시간 연결 확인 필요" : stock.watched ? "실시간 관찰 중" : reasons[stock.eligibility] || "확인 필요";
      card.append(node("p",status,stock.watched ? "stage-action" : "fine"),node("p",`시세 기준 · ${timeLabel(stock.as_of)}`,"fine")); root.append(card);
    }
    if(!data.items?.length) root.append(node("p",data.available ? "검색 결과가 없습니다. 종목명이나 코드를 바꿔보세요." : "국내 종목 목록이 준비되면 표시됩니다.","empty-state"));
    byId("market-page").textContent=`${page+1} / ${Math.max(1,Math.ceil(data.matched/40))}`;
    byId("market-prev").disabled=page===0;
    byId("market-next").disabled=!data.has_next;
  } catch {if(request===marketRequest) byId("market-notice").textContent="종목 목록을 불러오지 못했습니다. 검색을 다시 시도해주세요.";}
}
byId("view-market").addEventListener("click",()=>showView("market"));
byId("stock-search").addEventListener("input",()=>{clearTimeout(marketSearchTimer);marketRequest++;marketSearchTimer=setTimeout(()=>loadMarket(0),250);});
byId("stock-market").addEventListener("change",()=>loadMarket(0));
byId("market-prev").addEventListener("click",()=>loadMarket(Math.max(0,marketPage-1)));
byId("market-next").addEventListener("click",()=>loadMarket(marketPage+1));
byId("report-symbol").addEventListener("input",()=>{reportSymbol=byId("report-symbol").value.trim().toLowerCase();renderAnalysis(lastAnalysis);});

let manualSaving=false, manualRequestId="";
const inputRetries=new Map();
const expandedRuns=new Set(), fullRunCache=new Map(), fullRunPanels=new Map();
byId("manual-instruction")?.addEventListener("input",()=>{if(!manualSaving) manualRequestId="";});
byId("submit-instruction")?.addEventListener("click",async()=>{
  const input=byId("manual-instruction"), result=byId("manual-result");
  if(manualSaving) return;
  const instruction=input.value.trim();
  if(!instruction) { result.textContent="제안할 내용을 먼저 입력해주세요."; input.focus?.(); return; }
  manualSaving=true; input.disabled=true; byId("submit-instruction").disabled=true;
  manualRequestId ||= crypto.randomUUID().replaceAll("-","");
  result.textContent="판단을 시작할 수 있는지 확인하고 있습니다…";
  try {
    await api("/v1/analysis/manual",{instruction,request_id:manualRequestId});
    input.value=""; manualRequestId="";
    result.textContent="판단을 시작했습니다. 정리 후 의사결정하며, 결과는 분석 기록에 남습니다.";
    await sync();
  } catch(error) {
    if(error.status===409) manualRequestId="";
    result.textContent=error.status===409 ? "진행 중인 판단이나 미해결 주문이 있어 시작하지 못했습니다. 입력한 내용은 유지됩니다." : "접수 결과를 확인하지 못했습니다. 분석 기록을 확인한 뒤 다시 시도해주세요.";
  } finally {manualSaving=false;input.disabled=false;byId("submit-instruction").disabled=Boolean(lastAnalysis?.active || lastAnalysis?.orders_blocked);}
});

async function loadOlderAnalysis() {
  if(historyLoading || historyEnd) return;
  const generation=historyGeneration;
  historyLoading=true; renderHistoryButton();
  try {
    const page=await api("/v1/analysis/history"+(historyCursor ? `?cursor=${encodeURIComponent(historyCursor)}` : ""));
    if(generation!==historyGeneration) return;
    for(const record of page.records) historyRecords.set(record.id,record);
    historyCursor=page.next_cursor; historyStarted=true; historyEnd=!page.next_cursor;
    reportKey="";
    if(lastAnalysis) renderDecisionPipeline(lastAnalysis);
  } catch(error) { if(generation===historyGeneration) notice("이전 기록을 불러오지 못했습니다. 다시 시도해주세요."); }
  finally { if(generation===historyGeneration) {historyLoading=false;renderHistoryButton();} }
}
function renderHistoryButton() {
  let button=byId("load-older-analysis");
  if(!button) {button=node("button","이전 기록 더 보기");button.id="load-older-analysis";byId("reports").after(button);button.addEventListener("click",loadOlderAnalysis);}
  button.hidden=historyEnd;
  button.disabled=historyLoading;
  button.textContent=historyLoading ? "이전 기록 불러오는 중…" : "이전 기록 더 보기";
}
function renderDecisionPipeline(data) {
  const book=data.memory_book;
  byId("memory-book-version").textContent=book?.revision ? `· ${book.revision}번째 갱신` : "· 첫 갱신 전";
  byId("memory-book-content").textContent=book?.content || "첫 판단을 기다리고 있습니다.";
  byId("memory-book-updated").textContent=book?.origin === "legacy_seed" ? "기존 판단의 짧은 요약으로 시작합니다. 다음 의사결정이 완료되면 AI가 다시 정리합니다." : book?.updated_at ? `최근 갱신 ${timeLabel(book.updated_at)}` : "완료된 의사결정이 아직 없습니다.";
  if(byId("web-search-status")) byId("web-search-status").textContent=`웹검색 · 정리 ${data.sources?.web_provider || "확인 중"} · 의사결정 ${data.sources?.decision_web_provider || "확인 중"}`;
  renderOptionalConnections();
  const groups=data.usage?.groups || [], total=groups.reduce((n,g)=>n+g.attempts,0), failed=groups.reduce((n,g)=>n+g.failed,0), unknown=groups.reduce((n,g)=>n+g.unknown_usage,0), tokens=groups.reduce((n,g)=>n+g.input_tokens+g.output_tokens,0);
  const blocked=groups.reduce((n,g)=>n+(g.blocked_before_provider || 0),0);
  byId("analysis-usage").textContent=`오늘 AI 요청 ${total}회 · 실패 ${failed}회 · 확인된 ${tokens.toLocaleString("ko-KR")} tokens${unknown ? ` · 사용량 미확인 ${unknown}회` : ""}${blocked ? ` · 입력 상한으로 호출 전 차단 ${blocked}회` : ""}${data.surveillance_retry_at>Date.now()/1000 ? ` · 감시 연결 재확인 ${timeLabel(data.surveillance_retry_at)}` : ""}. 업데이트 이후 집계이며 청구 금액과는 다를 수 있습니다.`;
  const labels={starting:"준비 중",analyzing:"판단 중",orders_pending:"주문 확인 대기",observing:"감시 중",outside_session:"다음 정규장 대기",waiting_for_context:"연결·설정 확인 필요"};
  byId("analysis-state").textContent=labels[data.state] || "준비 중";
  byId("analysis-budget").textContent="정리 최대 16회 · 자료 조회 6회 · 정리 조사 2회 · 의사결정 직접 검색 가능 · 단계별 10분";
  const next=(data.schedule || []).filter(s=>s.at>Date.now()/1000).sort((a,b)=>a.at-b.at)[0];
  byId("home-analysis").textContent=data.active ? "정리와 의사결정이 진행 중입니다. 추가 긴급 증거는 합쳐서 다시 검토합니다." : next ? `다음 정시 판단 · ${timeLabel(next.at)}` : "정시 판단은 장중 2회입니다. 토스의 정규장 시간을 따릅니다.";
  byId("manual-state").textContent=data.active ? "판단 중" : data.orders_blocked ? "주문 확인 대기" : "판단 제안";
  byId("submit-instruction").disabled=manualSaving || data.active || data.orders_blocked;
  byId("analysis-role-help").textContent="감시의 긴급 신호는 정리에서 재평가합니다. 정리도 긴급할 때만 즉시 의사결정하며, 정시 판단은 장중 2회입니다.";
  for(const role of ["all",...Object.keys(roleLabels)]) byId(`filter-${role}`).setAttribute?.("aria-pressed",String(reportFilter===role));
  for(const record of data.layers || []) historyRecords.set(record.id,record);
  if(!historyStarted && Object.prototype.hasOwnProperty.call(data,"history_cursor")) {historyCursor=data.history_cursor;historyEnd=!data.history_cursor;}
  renderHistoryButton();
  const key=JSON.stringify([data.layers,[...historyRecords.values()],data.runs,data.surveillance,reportFilter,reportSymbol,data.sources,data.active,data.orders_blocked]);
  if(reportKey===key) return; reportKey=key;
  const root=byId("reports"); root.replaceChildren(); fullRunPanels.clear();
  if(data.sources?.web!=="connected" || data.sources?.dart!=="connected") {
    root.append(node("p",`자료 연결 · 웹검색 ${data.sources?.web==="connected" ? `${data.sources?.web_provider || "웹검색"} 사용 가능` : "연결 확인 필요"} · DART ${data.sources?.dart==="connected" ? "연결됨" : "미연결 또는 조회 실패"}`,"connection-note"));
  }
  const stateLabels={historical:"이전 기록",running:"진행 중",complete:"완료",aborted:"중단",interrupted:"재시작으로 중단",skipped_busy:"진행 중인 판단으로 건너뜀",skipped_orders:"주문 확인 대기",verified:"점검 완료",verification_failed:"점검 중단"};
  const kinds={legacy:"이전 분석",manual:"사용자 지시",scheduled:"정시 판단",critical:"긴급 재검토",verification:"운영 점검"};
  // Compatibility for old status snapshots; current servers persist independent records.
  const records=Array.isArray(data.layers) ? [...new Map([...historyRecords.values(),...data.layers].map(r=>[r.id,r])).values()] : [
    ...(data.surveillance || []).map((s,i)=>({...s,id:`${s.id || i}:cheap`,role:"cheap",started:s.as_of,state:"complete"})),
    ...(data.runs || []).flatMap(run=>{
      const records=[], traces=run.trace || [];
      const middle=run.brief || traces.some(t=>t.role==="middle") || !run.decision;
      const research=traces.some(t=>t.role==="research") || (run.decision && !(run.kind==="critical" && ["NORMAL","WARN"].includes(run.brief?.severity)));
      if(middle) records.push({...run,id:`${run.id}:middle`,run_id:run.id,role:"middle",decision:null,state:run.brief ? "complete" : run.state,error:run.brief ? null : run.error,trace:traces.filter(t=>t.role==="middle")});
      if(research) records.push({...run,id:`${run.id}:research`,run_id:run.id,role:"research",brief:null,trace:traces.filter(t=>t.role==="research")});
      return records;
    })
  ];
  records.sort((a,b)=>(b.started || 0)-(a.started || 0) || a.id.localeCompare(b.id));
  for(const record of records) {
    const role=record.role;
    if(reportFilter!=="all" && reportFilter!==role) continue;
    if(reportSymbol && !JSON.stringify([record.brief?.candidates,record.decision?.intents,record.instruction,record.symbols]).toLowerCase().includes(reportSymbol)) continue;
    const deferred=role==="middle" && record.kind==="critical" && record.state==="complete" && ["NORMAL","WARN"].includes(record.brief?.severity);
    const card=node("details",undefined,`report layer-${role}`);
    card.setAttribute("data-layer-id",record.id);
    card.open=expandedRuns.has(record.id);
    card.addEventListener("toggle",()=>{if(card.open) expandedRuns.add(record.id);else expandedRuns.delete(record.id);});
    card.append(node("summary",`${roleLabels[role]} · ${deferred ? "정리에서 종료" : stateLabels[record.state] || record.state} · ${timeLabel(record.started)}`));
    if(role==="cheap") {
      card.append(node("p",record.severity,"stage-label"),node("p",record.summary));
      for(const trace of record.trace || []) appendInputMeasurement(card,trace);
      root.append(card);continue;
    }
    card.append(node("p",`${kinds[record.kind] || "분석"} · 요청 ${timeLabel(record.requested_at || record.started)}`,"fine"));
    if(record.instruction) card.append(node("h3","사용자 제안"),node("p",record.instruction));
    if(role==="middle") {
      const stage=node("section",undefined,"analysis-stage analysis-middle");
      stage.append(node("h3","정리 계층 · 근거와 후보"));
      if(record.brief) {
        stage.append(node("p",`시장 증거의 긴급도 · ${record.brief.severity}`,"stage-label"),node("p",record.brief.summary));
        if(record.kind==="critical") stage.append(node("p",deferred ? "의사결정 AI 미호출 · 정리 결과를 다음 판단에 보관합니다." : "긴급성 확인 · 의사결정으로 전달","stage-action"));
        const candidates=node("details");candidates.append(node("summary",`검토 후보 ${record.brief.candidates.length}종목`));
        for(const c of record.brief.candidates) candidates.append(node("p",`${c.symbol} · ${c.reason}`,"fine"));
        stage.append(candidates);
      } else stage.append(node("p",record.state==="running" ? "증거와 후보를 검토하고 있습니다." : "정리 결과가 생성되지 않았습니다.","muted"));
      card.append(stage);
    } else {
      const stage=node("section",undefined,"analysis-stage analysis-research");
      stage.append(node("h3","의사결정 계층 · 최종 결론"));
      if(record.decision) {
        const decision=record.decision;
        stage.append(node("p",decision.action==="SUBMIT" ? "매매 제안" : "거래하지 않음","stage-label"),node("p",decision.summary,"decision-summary"));
        for(const i of decision.intents || []) stage.append(node("p",`${i.symbol} · ${i.side==="BUY" ? "매수" : "매도"} ${i.quantity}주 · 지정가 ${won(i.limit_price)}`),node("p",i.rationale,"fine"));
        const caveats=node("details");caveats.append(node("summary","반대 근거와 불확실성"),node("h3","반대 근거"),node("p",decision.counterargument),node("h3","불확실성"),node("p",decision.uncertainty));stage.append(caveats);
      } else stage.append(node("p",record.state==="running" ? "최신 계좌와 근거를 검토하고 있습니다." : "최종 의사결정이 생성되지 않았습니다.","muted"));
      card.append(stage);
    }
    if(record.input_limit_override) card.append(node("p","사용자 요청 · 이 판단만 입력 크기 한도 해제","fine"));
    if(record.error) card.append(node("p",analysisFailureMessage(record.error)+" 이번 판단으로 새 주문을 만들지 않습니다.","risk-note"));
    if(record.error==="input_budget_exceeded" && record.state==="aborted") renderInputRetry(card,{...record,id:record.run_id},data);
    const button=node("button",role==="middle" ? "정리 상세와 근거 보기" : "의사결정 상세와 근거 보기","secondary"), detail=node("div");
    fullRunPanels.set(record.id,{detail,button});
    button.addEventListener("click",async()=>{
      button.disabled=true;
      try {
        const full=await api(`/v1/analysis/${record.run_id}/layers/${role}`);
        renderFullDecision(detail,full.data,role);button.hidden=true;
        if(record.state!=="running") {
          fullRunCache.set(record.id,full.data);
          if(fullRunCache.size>2) {const id=fullRunCache.keys().next().value;fullRunCache.delete(id);const old=fullRunPanels.get(id);if(old){old.detail.replaceChildren();old.button.hidden=false;old.button.disabled=false;}}
        }
      } catch {detail.replaceChildren(node("p","기록을 가져오지 못했습니다. 다시 시도해주세요."));button.disabled=false;}
    });
    if(fullRunCache.has(record.id)) {renderFullDecision(detail,fullRunCache.get(record.id),role);button.hidden=true;}
    card.append(button,detail);root.append(card);
  }
  if(!root.children.length) root.append(node("p","첫 판단을 기다리고 있습니다.","empty-state"));
}

function renderInputRetry(card,run,data) {
  const state=inputRetries.get(run.id) || {requestId:"",pending:false,accepted:false,message:""};
  inputRetries.set(run.id,state);
  const retry=node("button","입력 한도 없이 다시 판단","secondary"), result=node("p",state.message,"form-notice");
  retry.id=`retry-input-${run.id}`;result.id=`retry-input-result-${run.id}`;
  result.setAttribute("role","status");
  const existing=(data.runs || []).find(r=>r.retry_of===run.id && !["skipped_busy","skipped_orders"].includes(r.state));
  if(existing) state.accepted=true;
  if(state.accepted && !state.message) result.textContent="재실행 요청이 접수되었습니다. 새 판단 기록에서 확인하세요.";
  retry.disabled=state.pending || state.accepted || data.active || data.orders_blocked;
  retry.textContent=state.accepted ? "재실행 접수됨" : state.pending ? "접수 중…" : "입력 한도 없이 다시 판단";
  card.append(node("p","이 판단만 입력 크기 제한을 해제하고 최신 자료로 처음부터 다시 분석합니다. AI 비용이 늘 수 있으며, 실거래 활성화 상태에서는 주문으로 이어질 수 있습니다.","fine"),retry,result);
  retry.addEventListener("click",async()=>{
    if(state.pending || state.accepted) return;
    state.pending=true;retry.disabled=true;state.requestId ||= crypto.randomUUID().replaceAll("-","");
    try {
      await api(`/v1/analysis/${run.id}/retry-input-limit`,{request_id:state.requestId});
      state.accepted=true;state.message="재실행 요청이 접수되었습니다. 새 판단 기록에서 확인하세요.";
      retry.textContent="재실행 접수됨";await sync();
    } catch(error) {
      if(error.status===409) state.requestId="";
      state.message=error.status===409 ? "지금은 시작할 수 없습니다. 진행 중인 판단·주문과 운용 상태를 확인해주세요." : "접수 여부를 확인하지 못했습니다. 다시 눌러도 같은 요청으로 확인합니다.";
    } finally {
      state.pending=false;result.textContent=state.message;
      const shown=byId(`retry-input-result-${run.id}`);if(shown) shown.textContent=state.message;
      const current=byId(`retry-input-${run.id}`);if(current) {current.disabled=state.accepted || lastAnalysis?.active || lastAnalysis?.orders_blocked;current.textContent=state.accepted ? "재실행 접수됨" : "입력 한도 없이 다시 판단";}
    }
  });
}

function inputSize(bytes) {
  if(!Number.isFinite(bytes)) return "미기록";
  return bytes<1000 ? `${bytes.toLocaleString("ko-KR")} B` : `${(bytes/1000).toLocaleString("ko-KR",{maximumFractionDigits:1})} KB`;
}

function appendInputMeasurement(root,trace) {
  const context=trace.input_context;
  if(context) {
    root.append(node("p",`${trace.provider_called===false ? "차단된 요청 입력" : "AI 입력"} ${inputSize(context.total_bytes)} · 호출당 상한 ${inputSize(context.budget_bytes)}`,trace.provider_called===false ? "risk-note" : "fine"));
    root.append(node("p",`자료 ${inputSize(context.payload_bytes)} · 시스템 지시 ${inputSize(context.system_bytes)} · 응답 형식 ${inputSize(context.schema_bytes)}`,"fine"));
    if(context.limit_overridden) root.append(node("p","사용자 요청으로 이 호출의 입력 크기 한도를 해제했습니다.","fine"));
    if(trace.provider_called===false) root.append(node("p","입력 상한을 초과해 AI 호출 전에 차단했습니다.","fine"));
    const fields=Object.fromEntries(Object.entries(context.fields || {}).sort((a,b)=>(b[1].bytes || 0)-(a[1].bytes || 0)).map(([key,value])=>[key,inputSize(value.bytes)]));
    if(Object.keys(fields).length) appendStructured(root,"항목별 입력 크기",fields);
  }
  if(Number.isFinite(trace.input_tokens) && Number.isFinite(trace.output_tokens)) root.append(node("p",`제공자가 확인한 사용량 · 입력 ${trace.input_tokens.toLocaleString("ko-KR")} · 출력 ${trace.output_tokens.toLocaleString("ko-KR")} tokens`,"fine"));
  else if(trace.provider_called!==false) root.append(node("p","제공자 사용량 미확인 · 요청 결과가 아직 확인되지 않았을 수 있습니다.","fine"));
}

function renderInputAudit(root,data) {
  const section=node("details");section.append(node("summary","AI에 전달한 입력"));
  const measured=(data.trace || []).filter(t=>Number.isFinite(t.input_context?.total_bytes));
  if(!measured.length) section.append(node("p","이 기록에는 입력 크기가 저장되지 않았습니다. 업데이트 이후 기록부터 확인할 수 있습니다.","fine"));
  for(const role of Object.keys(roleLabels)) {
    const calls=measured.filter(t=>t.role===role && t.provider_called!==false), rejected=measured.filter(t=>t.role===role && t.provider_called===false);
    if(calls.length || rejected.length) section.append(node("p",`${roleLabels[role]} · ${calls.length}회 · 입력 합계 ${inputSize(calls.reduce((sum,t)=>sum+t.input_context.total_bytes,0))}${rejected.length ? ` · 호출 전 차단 ${rejected.length}회` : ""}`));
  }
  section.append(node("p","입력 크기에는 시스템 지시와 응답 형식이 포함됩니다. 웹검색 제공자가 내부에서 읽은 자료는 제공자의 실제 사용량에 따로 반영될 수 있습니다.","fine"));
  const snapshots=(data.model_inputs || []).map(value=>({...value,used:false}));
  const taskNames={review_batch:"후보 검토",merge_decision_brief:"검토 통합",investment_decision:"최종 판단",news_research:"뉴스 조사",web_search:"웹검색",web_search_summary:"검색 정리",surveillance:"감시"};
  let index=0;
  for(const trace of data.trace || []) {
    const snapshot=snapshots.find(s=>!s.used && s.role===trace.role && s.task===trace.task);
    if(snapshot) snapshot.used=true;
    if(!snapshot && !trace.input_context) continue;
    const block=node("details");block.append(node("summary",`${++index}. ${roleLabels[trace.role] || "AI"} · ${taskNames[trace.task] || trace.task || "판단"}${trace.provider_called===false ? " · 호출 전 차단" : ""}`));
    appendInputMeasurement(block,trace);
    if(snapshot) appendStructured(block,trace.provider_called===false ? "차단된 입력 내용" : "AI 입력 요청 내용",snapshot.payload);
    section.append(block);
  }
  for(const snapshot of snapshots.filter(s=>!s.used)) appendStructured(section,`${roleLabels[snapshot.role] || "AI"} · 입력 요청 내용 · 전달 여부 미확인`,snapshot.payload);
  root.append(section);
}

function renderFullDecision(root,data,role="all") {
  root.replaceChildren();
  const visible=role==="all" ? data : {...data,trace:(data.trace || []).filter(t=>t.role===role),model_inputs:(data.model_inputs || []).filter(t=>t.role===role)};
  if(role!=="middle" && data.decision?.detailed_explanation) {
    root.append(node("h3","의사결정 상세 설명"),node("p",data.decision.detailed_explanation,"prompt-preview"));
  }
  if(data.error) {
    root.append(node("p",analysisFailureMessage(data.error),"risk-note"));
    appendStructured(root,"중단 원인 상세",{code:data.error,...(data.context_failure || {})});
  }
  for(const [key,label] of [["memory_book_before","판단에 사용한 메모리북"],["memory_book_after","이번 판단이 남긴 메모리북"]]) {
    if(role!=="middle" && data[key]) {const block=node("details");block.append(node("summary",label),node("p",data[key].content,"prompt-preview"));root.append(block);}
  }
  if(role!=="middle" && data.intervening_briefs?.length) appendStructured(root,"메모리북 이후 검토한 정리 요약",data.intervening_briefs);
  renderInputAudit(root,visible);
  if(data.configuration) appendStructured(root,"이 판단에 적용한 모델과 전략",data.configuration);
  for(const trace of visible.trace || []) {
    const block=node("details");block.append(node("summary",`${roleLabels[trace.role]} · ${modelName(trace.model || "")} · ${trace.reasoning || ""}`));
    if(trace.decision?.summary) block.append(node("p",trace.decision.summary));
    if(trace.decision) appendStructured(block,"검증 가능한 판단 출력",trace.decision);
    appendInputMeasurement(block,trace);root.append(block);
  }
  const archive=node("details");archive.append(node("summary",data.role ? "이 계층의 보관 자료" : "전체 계층의 보관 원본 자료"));
  onFirstOpen(archive,()=>{
    archive.append(node("p","수집·보관한 전체 자료입니다. 실제 AI 입력은 위의 요청별 기록에서 확인할 수 있습니다.","fine"));
    renderArchivedDecision(archive,data);
  });root.append(archive);
}

function renderArchivedDecision(root,data) {
  const citations=Object.fromEntries(Object.entries(data.citation_aliases || {}).map(([alias,id])=>[id,alias]));
  if(data.legacy_report) appendStructured(root,"업데이트 전 분석 원본 · 재주문하지 않음",data.legacy_report);
  const context=data.decision_context || data.initial_context;
  if(context) {
    const section=node("details");section.append(node("summary","판단 시점의 계좌와 위험 한도"),node("p",`기준 ${timeLabel(context.as_of)} · 현금 ${won(context.cash)}`));
    for(const h of context.holdings || []) section.append(node("p",`${h.symbol} · 보유 ${h.quantity}주 · AI 운용 ${h.managed_quantity}주`));
    for(const [k,v] of Object.entries(context.risk_limits || {})) section.append(node("p",`${limitLabels[k] || k} · ${won(v)}`));
    appendStructured(section,"최신 계좌·주문 제약 전체",context);
    root.append(section);
  }
  if(data.initial_context?.comparison_table) {
    const section=node("details"), comparison=data.initial_context.comparison_table;
    section.append(node("summary",`전체 시장 비교표 · ${comparison.rows.length}종목`));
    if(comparison.krx_as_of) section.append(node("p",`KRX 통계 기준일 · ${comparison.krx_as_of}`,"fine"));
    onFirstOpen(section,()=>{
    const table=node("table"), head=node("tr");
    for(const c of ["종목 코드","종목명","시장","거래 상태","시세","시세 기준",...(comparison.columns?.length>6 ? ["KRX 시가총액"] : [])]) head.append(node("th",c));
    table.append(head);
    let offset=0; const more=node("button","다음 100종목 보기","secondary");
    const appendRows=()=>{for(const values of comparison.rows.slice(offset,offset+100)) {const tr=node("tr");for(let i=0;i<values.length;i++) tr.append(node("td",i===5 ? timeLabel(values[i]) : String(values[i] ?? "자료 없음")));table.append(tr);}offset+=100;more.hidden=offset>=comparison.rows.length;};
    more.addEventListener("click",appendRows);appendRows();
    const scroll=node("div",undefined,"table-scroll");scroll.append(table);section.append(scroll,more);
    });root.append(section);
  }
  if(data.previous_investment_rationales?.length) appendStructured(root,"이전 투자 근거",data.previous_investment_rationales);
  if(data.tool_results?.length) appendStructured(root,"추가 조회 질문과 결과",data.tool_results);
  for(const news of data.grounded_news || []) renderGroundedNews(root,news);
  for(const evidence of data.evidence || []) {
    const block=node("details");block.append(node("summary",`${citations[evidence.id] || evidence.id || ""}${evidence.id ? " · " : ""}${evidence.title || `${evidence.source || evidence.kind} · ${evidence.symbol || ""}`}`));
    onFirstOpen(block,()=>{
    block.append(node("p",`수집 ${timeLabel(evidence.collected_at || evidence.as_of)} · ${evidence.status || "저장된 자료"}`,"fine"));
    if(evidence.url && /^https:\/\//.test(evidence.url)) {const link=node("a","출처 원문 보기");link.href=evidence.url;link.target="_blank";link.rel="noopener noreferrer";block.append(link);}
    if(evidence.excerpt) block.append(node("p",evidence.excerpt));
    if(evidence.page_excerpt) block.append(node("p",evidence.page_excerpt));
    for(const field of ["quote","orderbook","trades","warnings","warning","signals"]) if(evidence[field]) appendStructured(block,({quote:"동결된 시세",orderbook:"동결된 호가",trades:"동결된 체결",warnings:"거래 경고",warning:"거래 경고",signals:"감지 조건"})[field],evidence[field]);
    if(evidence.daily_bars?.length) {const table=node("table");for(const b of evidence.daily_bars) {const tr=node("tr");for(const k of ["date","open","high","low","close","volume"]) tr.append(node("td",String(b[k] ?? "—")));table.append(tr);}const scroll=node("div",undefined,"table-scroll");scroll.append(table);block.append(scroll);}
    });root.append(block);
  }
}

function analysisFailureMessage(code) {
  const component=/^context_(daily_bars|orderbook|trades|warnings)_/.exec(code || "");
  const name=component ? ({daily_bars:"일봉",orderbook:"호가",trades:"체결",warnings:"거래 경고"})[component[1]]+" 자료" : "자료";
  if(/toss_http_429$/.test(code)) return `토스 조회 한도로 ${name} 수집을 완료하지 못했습니다.`;
  if(/toss_http_40[13]$/.test(code)) return "토스 연결 인증 또는 접근 권한을 확인하지 못했습니다.";
  if(/invalid_ohlc|invalid_candle|duplicate_candle/.test(code)) return "일봉 가격이나 날짜의 일관성을 확인하지 못해 판단을 중단했습니다.";
  if(component && /context_timeout$/.test(code)) return `${name} 응답이 지연되어 판단을 중단했습니다.`;
  if(["context_timeout","context_collection_timeout","TimeoutError"].includes(code)) return "제한 시간 안에 자료 수집 또는 분석을 완료하지 못했습니다.";
  if(/^account_changed_/.test(code)) return "분석 중 계좌나 주문 상태가 바뀌어 판단을 중단했습니다.";
  if(code==="provider_timeout") return "AI 제공자의 응답이 지연되어 이번 호출을 중단했습니다. 중복 비용을 피하기 위해 같은 요청을 자동으로 재전송하지 않습니다.";
  if(code==="settings_changed") return "분석 중 투자 설정이 변경되어 판단을 중단했습니다.";
  if(code==="unverified_citation") return "AI가 제시한 근거를 저장된 자료에서 확인하지 못했습니다.";
  if(code==="context_unavailable") return "판단에 필요한 자료를 수집하지 못했습니다.";
  if(code==="input_budget_exceeded") return "AI 입력이 정해진 크기를 초과해, 비용이 발생하는 호출 전에 차단했습니다.";
  return "자료·계좌 변화 또는 응답 검증 문제로 판단을 중단했습니다.";
}

function onFirstOpen(section,render) {
  let rendered=false;
  section.addEventListener("toggle",()=>{if(section.open && !rendered) {rendered=true;render();}});
}

function appendStructured(root,label,value) {
  const section=node("details");section.append(node("summary",label));
  const labels={symbol:"종목 코드",quantity:"수량",managed_quantity:"AI 운용 수량",cash:"주문 가능 현금",as_of:"기준 시각",risk_limits:"위험 한도",risk_status:"위험 상태",open_orders:"미체결 주문",holdings:"보유 종목",quotes:"최신 시세",order_constraints:"주문 제약",price:"가격",volume:"거래량",timestamp:"자료 시각",bids:"매수 호가",asks:"매도 호가",summary:"판단 근거",evidence_ids:"근거 ID",candidates:"후보 종목",uncertainties:"미확인 사항",reason:"선정 이유",reasoning:"추론 설정",strategy:"투자 전략",prompt:"전략 프롬프트",models:"AI 모델",action:"최종 결과",intents:"매매 제안",daily_pnl_krw:"당일 손익",unresolved_submission:"미해결 주문 제출",side:"매매 방향",limit_price:"지정가",rationale:"투자 근거",tool:"조회 도구",arguments:"조회 요청",result:"조회 결과",sources:"조회 출처",source:"자료 제공자",collected_at:"수집 시각",url:"원문 주소",excerpt:"검색 결과 발췌",page_excerpt:"원문 발췌",sellable_quantity:"매도 가능 수량",settings_revision:"설정 버전",detailed_explanation:"상세 설명",counterargument:"반대 근거",uncertainty:"불확실성",evidence:"저장된 증거"};
  Object.assign(labels,{account:"최신 계좌 요약",brief:"정리 결과",stocks:"검토 종목 지표",recent_evidence:"최근 증거 요약",batches:"묶음별 검토 요약",market_coverage:"시장 검토 범위",mandatory_evidence:"필수 공시·경고 요약",evidence_index:"조회 가능한 증거 목록",previous_investment_rationales:"이전 투자 근거 요약",manual_instruction:"사용자 지시",tool_results:"추가 조회 요약",retrieval_history:"이전 조회 목록",signals:"감지 조건 요약",schema:"응답 형식",tool_schema:"조회 도구 형식",context_policy:"자료 사용 지침",citation_policy:"근거 인용 지침",allowed_candidate_symbols:"선택 가능한 종목",available_evidence_ids:"사용 가능한 근거 ID"});
  onFirstOpen(section,()=>{
  if(value && typeof value==="object") {
    const entries=Array.isArray(value) ? value.map((v,i)=>[String(i+1),v]) : Object.entries(value);
    if(!entries.length) section.append(node("p","해당 자료 없음","fine"));
    for(const [key,item] of entries) {
      const name=labels[key] || limitLabels[key] || roleLabels[key] || key;
      if(item && typeof item==="object") appendStructured(section,name,item);
      else section.append(node("p",`${name} · ${item===null ? "자료 없음" : typeof item==="boolean" ? item ? "예" : "아니요" : String(item)}`,"fine"));
    }
  } else section.append(node("p",String(value ?? "자료 없음")));
  });root.append(section);
}

var optionalChoice="dart", optionalSaving=false;
const optionalCopy={dart:{name:"DART",help:"금융감독원의 공식 공시 목록을 연결합니다. 중요 공시를 감지하고 판단 근거에 더합니다.",url:"https://opendart.fss.or.kr/"},krx:{name:"KRX",help:"코스피·코스닥의 일별 종가, 거래량, 거래대금, 시가총액을 더합니다. KRX에서 인증키 발급과 ‘유가증권 일별매매정보’, ‘코스닥 일별매매정보’ 두 API의 이용 승인을 받아주세요. 실시간 주문 가격은 토스 시세를 사용합니다.",url:"https://openapi.krx.co.kr/"}};
function renderOptionalConnections() {
  const states=[];
  for(const p of ["dart","krx"]) {
    const saved=providerStatus[p]?.connected, live=lastAnalysis?.sources?.[p], name=p.toUpperCase();
    const label=saved ? live==="connected" ? "연결됨" : live==="unavailable" ? "조회 확인 필요" : "연결됨 · 조회 대기" : "선택 연결";
    states.push(`${name} ${label}`);
    byId(`home-${p}`).textContent=`${name} ${saved ? "연결 관리" : "연결"}`;
    byId(`settings-${p}-state`).textContent=label+(p==="krx" && saved && lastAnalysis?.sources?.krx_as_of ? ` · 기준 ${lastAnalysis.sources.krx_as_of}` : "");
    byId(`settings-${p}`).textContent=saved ? "키 변경" : "연결";
    byId(`disconnect-${p}`).hidden=!saved;
  }
  byId("optional-summary").textContent=states.join(" · ");
  byId("optional-banner").hidden=Boolean(providerStatus.dart?.connected && providerStatus.krx?.connected);
}
function openOptional(provider) {
  if(optionalSaving) return;
  optionalChoice=provider; clearProviderKey();
  const copy=optionalCopy[provider];
  byId("optional-title").textContent=`${copy.name} 연결`;
  byId("optional-help").textContent=copy.help;byId("optional-guide").href=copy.url;
  byId("optional-key-label").textContent=`${copy.name} API 키`;
  byId("optional-notice").textContent="";
  byId(currentView==="settings" ? "settings-optional-slot" : "home-optional-slot").append(byId("optional-panel"));
  byId("optional-panel").hidden=false;byId("optional-panel").scrollIntoView?.({behavior:"smooth",block:"center"});
}
for(const p of ["dart","krx"]) {
  byId(`home-${p}`).addEventListener("click",()=>{if(providerStatus[p]?.connected) showView("settings");else openOptional(p);});
  byId(`settings-${p}`).addEventListener("click",()=>openOptional(p));
  byId(`disconnect-${p}`).addEventListener("click",async()=>{
    if(optionalSaving) return;optionalSaving=true;byId(`disconnect-${p}`).disabled=true;
    try {const result=await api("/v1/providers/disconnect",{provider:p},110000);if(!connected || suspended) return;renderProviders(result.provider_connections);byId("optional-settings-notice").textContent=`${p.toUpperCase()} 연결을 해제했습니다. 기본 분석은 계속 사용할 수 있습니다.`;byId("optional-panel").hidden=true;clearProviderKey();await sync();}
    catch {byId("optional-settings-notice").textContent="연결 해제를 확인하지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해주세요.";}
    finally {optionalSaving=false;byId(`disconnect-${p}`).disabled=false;}
  });
}
byId("optional-close").addEventListener("click",()=>{byId("optional-panel").hidden=true;clearProviderKey();});
function dartConnectionMessage(error) {
  const messages={
    dart_key_unregistered:"DART에 등록되지 않은 인증키입니다. 인증키 관리 화면에서 키를 다시 복사해주세요. (010)",
    dart_key_disabled:"DART 인증키가 사용 중지 상태입니다. 인증키 관리 화면에서 사용 상태를 확인해주세요. (011)",
    dart_ip_denied:"DART가 서버의 접속 IP를 허용하지 않았습니다. 인증키의 접속 IP 설정을 확인해주세요. (012)",
    dart_rate_limited:"DART 요청 한도에 도달했습니다. 한도가 회복된 뒤 다시 연결해주세요. (020)",
    dart_account_expired:"DART 계정의 개인정보 보유기간이 만료됐습니다. DART에서 계정 상태를 확인해주세요. (901)",
    dart_maintenance:"DART가 시스템 점검 중입니다. 잠시 후 다시 연결해주세요. (800)",
    dart_access_denied:"DART가 이 요청의 접근을 거절했습니다. (101)",
    dart_invalid_request:"DART가 조회 요청을 처리하지 못했습니다. 앱의 연결 오류로 기록했습니다.",
    dart_timeout:"DART 서버 응답 시간이 초과됐습니다. 잠시 후 다시 연결해주세요.",
    dart_connection_unavailable:"DART 서버와 통신하지 못했습니다. 키 오류로 확인된 것은 아닙니다. 잠시 후 다시 연결해주세요.",
    dart_invalid_response:"DART 응답 형식을 확인하지 못했습니다. 앱의 연결 오류로 기록했습니다.",
    dart_unavailable:"DART가 정상 응답을 반환하지 않았습니다. 잠시 후 다시 연결해주세요."
  };
  const text=messages[error.message] || "DART 연결 결과를 확인하지 못했습니다. 연결 상태를 확인한 뒤 다시 시도해주세요.";
  return text+(error.status===422 ? (providerStatus.dart?.connected ? " 기존 연결은 유지됩니다." : " 입력한 키는 저장하지 않았습니다.") : "");
}
byId("connect-optional").addEventListener("click",async()=>{
  if(optionalSaving) return;
  let key=byId("optional-api-key").value.trim();const provider=optionalChoice;
  if(!/^[A-Za-z0-9_-]{20,512}$/.test(key)) {byId("optional-notice").textContent="API 키 전체를 확인해주세요.";return;}
  optionalSaving=true;clearProviderKey();byId("connect-optional").disabled=true;byId("optional-api-key").disabled=true;
  byId("optional-notice").textContent="공식 API에 연결해 접근 권한을 확인하고 있습니다…";
  try {const pending=api("/v1/providers/connect",{provider,api_key:key},110000);key="";const result=await pending;if(!connected || suspended) return;renderProviders(result.provider_connections);byId("optional-notice").textContent="연결했습니다. 다음 분석부터 자료가 추가됩니다.";await sync();}
  catch(error) {byId("optional-notice").textContent=provider==="krx" ? "연결을 확인하지 못했습니다. 키와 코스피·코스닥 두 API의 이용 승인 상태를 확인해주세요. 기존 연결은 유지됩니다." : dartConnectionMessage(error);}
  finally {key="";clearProviderKey();optionalSaving=false;byId("connect-optional").disabled=false;byId("optional-api-key").disabled=false;}
});
function renderGroundedNews(root,news) {
  const section=node("details",undefined,"report grounded-news");section.append(node("summary",`${news.provider==="gemini" ? "Google Search" : "OpenAI Web Search"} · 뉴스 조사 원문`));
  onFirstOpen(section,()=>{
    section.append(node("p",news.text, "prompt-preview"));
    for(const citation of news.citations || []) if(/^https:\/\//.test(citation.url || "")) {const link=node("a",citation.title || "검색 출처");link.href=citation.url;link.target="_blank";link.rel="noopener noreferrer";const line=node("p");line.append(link);section.append(line);}
    if(news.provider==="gemini" && news.search_suggestions) {
      // Provider HTML is never inserted into the app document. Strip executable content,
      // isolate CSS in a shadow tree, and keep Google suggestions beside its original answer.
      const parsed=new DOMParser().parseFromString(news.search_suggestions,"text/html");
      for(const e of parsed.querySelectorAll("script,iframe,object,embed,form,input,button,link,meta,base")) e.remove();
      for(const e of parsed.querySelectorAll("*")) {
        if(!["HTML","HEAD","BODY","DIV","SPAN","P","A","STYLE","SVG","PATH","G","CIRCLE","RECT","TITLE"].includes(e.tagName.toUpperCase())) {e.remove();continue;}
        for(const a of [...e.attributes]) if(/^on/i.test(a.name) || ["src","srcdoc","formaction","xlink:href"].includes(a.name)) e.removeAttribute(a.name);
        if(e.hasAttribute("href") && !/^https:\/\//.test(e.getAttribute("href"))) e.removeAttribute("href");
        if(e.tagName==="A") {e.target="_blank";e.rel="noopener noreferrer";}
        if(e.tagName==="STYLE" && /@import|url\s*\(|expression\s*\(/i.test(e.textContent)) e.remove();
        if(/url\s*\(|expression\s*\(/i.test(e.getAttribute("style") || "")) e.removeAttribute("style");
      }
      const host=node("div",undefined,"search-suggestions");const shadow=host.attachShadow({mode:"closed"});
      for(const e of [...parsed.head.children,...parsed.body.children]) shadow.append(e);
      section.append(host);
    }
  });root.append(section);
}

function renderPromptCounts() {
  for(const role of promptRoles) byId(role+"-prompt-count").textContent = `${Array.from(byId(role+"_prompt").value).length.toLocaleString("ko-KR")} / ${(rolePromptCatalog?.max_chars || 2000).toLocaleString("ko-KR")}`;
}
for(const role of promptRoles) {
  byId(role+"_prompt").addEventListener("input",()=>{renderPromptCounts();edited();});
  byId("reset-"+role+"-prompt").addEventListener("click",()=>{byId(role+"_prompt").value=rolePromptCatalog?.defaults?.[role] || "";renderPromptCounts();edited();});
}
byId("edit-role-prompts").addEventListener("click",()=>{openEditor("settings",3);byId("role-prompts-editor").open=true;byId("cheap_prompt").focus?.();});

byId("load-older-analysis")?.addEventListener("click",loadOlderAnalysis);

// The core is an illustration of the brand, not an implied return or progress meter.
function renderHero(data) {
  const policy=data.operating_policy, analysis=data.analysis, execution=data.execution;
  const age=Date.now()/1000-analysis?.updated_at;
  const fresh=Number.isFinite(analysis?.updated_at) && age>=-5 && age<90;
  const states={market_closed:["다음 거래 대기","waiting"],daily_loss_limit:["오늘 주문 중단","attention"],order_review:["주문 확인 필요","attention"]};
  let status=["상태 확인 중","waiting"];
  if(data.new_proposals_stopped) status=["운용 일시 중단","attention"];
  else if(!data.broker_connected) status=["연결 확인 필요","attention"];
  else if(policy?.live_requested && !policy.live_enabled) status=["실거래 준비 대기","waiting"];
  else if(policy && !policy.live_enabled) status=["관찰 모드","waiting"];
  else if(analysis?.orders_blocked) status=["주문 확인 대기","attention"];
  else if(states[execution?.state]) status=states[execution.state];
  else if(policy?.live_enabled) status=execution?.available ? ["실거래 활성","active"] : ["운용 상태 확인 중","waiting"];
  if(!data.new_proposals_stopped && data.broker_connected && fresh && analysis?.active) status=[policy?.live_enabled ? "AI 판단 중" : "관찰 · 판단 중","active"];
  byId("hero-status").textContent=status[0];
  byId("autonomy-hero").setAttribute("data-signal",status[1]);
  byId("hero-holdings").textContent=execution?.available ? `${(execution.managed_positions || []).length}종목` : "—";
  byId("hero-strategy").textContent=policy ? strategyName(policy.strategy?.preset || "stable")+" ↗" : "설정 전 ↗";
  const next=(analysis?.schedule || []).filter(s=>typeof s.at==="number" && s.at>Date.now()/1000).sort((a,b)=>a.at-b.at)[0];
  byId("hero-next").textContent=data.new_proposals_stopped ? "운용 재개 후" : next ? new Intl.DateTimeFormat("ko-KR",{timeZone:"Asia/Seoul",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).format(new Date(next.at*1000)) : analysis?.state==="outside_session" ? "다음 거래일 대기" : "일정 확인 중";
  for(const role of ["cheap","middle","research"]) {
    const running=!data.new_proposals_stopped && data.broker_connected && fresh && (analysis.layers || []).some(r=>r.role===role && r.state==="running");
    byId("hero-"+role).setAttribute("data-active",String(running));
    byId("hero-"+role+"-state").textContent=running ? "검토 중" : "기록 보기 ↗";
  }
}
function renderHeroPnl(data,loading=false) {
  const month=new Intl.DateTimeFormat("sv-SE",{timeZone:"Asia/Seoul",year:"numeric",month:"2-digit"}).format(new Date());
  if(latestMonthlyPnl?.month!==month) latestMonthlyPnl=null;
  const result=data?.monthly_performance, value=result?.pnl_krw;
  const valid=!loading && result?.state==="available" && result.month===month && typeof value==="string" && value.trim()!=="" && Number.isFinite(Number(value));
  if(valid) latestMonthlyPnl={month,amount:Number(value),value:Number(value).toLocaleString("ko-KR",{maximumFractionDigits:0})+"원",at:Number.isFinite(result.as_of) ? result.as_of : Date.now()/1000,valuationDate:result.valuation_date};
  const pnl=latestMonthlyPnl;
  byId("hero-pnl").textContent=pnl ? (pnl.amount>0 ? "+" : "")+pnl.value : "—";
  byId("hero-pnl").setAttribute("data-direction",pnl?.amount<0 ? "negative" : "neutral");
  byId("hero-pnl-loading").hidden=valid && !result.refreshing;
  byId("hero-pnl-note").textContent=pnl ? `${Number(month.slice(5))}월 1일부터 · 비용 반영 · ${pnl.valuationDate ? pnl.valuationDate.slice(5).replace("-","/")+" 종가" : new Intl.DateTimeFormat("ko-KR",{timeZone:"Asia/Seoul",hour:"2-digit",minute:"2-digit",hourCycle:"h23"}).format(new Date(pnl.at*1000))+" 확인"}` : result?.state==="month_baseline_required" ? "월초 평가 기준을 확인하고 있습니다." : "월간 운용 결과를 확인하고 있습니다.";
  byId("hero-performance").setAttribute("title","한국 날짜 기준 이번 달 실현·평가손익과 체결 비용을 합산합니다. 입출금·기존 보유 주식은 AI 수익에 포함하지 않습니다.");
}
byId("hero-performance").addEventListener("click",()=>{
  byId("order-history").open=true;
  byId("execution-card").scrollIntoView?.({block:"start"});
  byId("execution-card").focus?.({preventScroll:true});
});
byId("hero-strategy").addEventListener("click",()=>openEditor(latestPolicy?.onboarding_completed ? "settings" : "onboarding",latestPolicy?.onboarding_completed ? 3 : 1));
for(const role of ["cheap","middle","research"]) byId("hero-"+role).addEventListener("click",()=>{
  reportFilter=role;reportSymbol="";byId("report-symbol").value="";
  renderAnalysis(lastAnalysis);showView("reports");
  byId("filter-"+role).focus?.({preventScroll:true});
});

function clearBrokerKeys() {
  for(const id of ["broker-client-id","broker-client-secret","broker-account-seq"]) byId(id).value="";
}
function renderInstallation(data) {
  const required=Boolean(data.installation && !data.installation.broker_configured);
  byId("broker-screen").hidden=!required;
  if(required) {
    currentView="broker";
    for(const id of ["home","reports","market","settings","setup"]) byId(id+"-screen").hidden=true;
    byId("live-banner").hidden=true;
    byId("broker-egress").textContent=data.installation.ip;
  }
  document.body?.setAttribute?.("data-broker-setup",String(required));
  return required;
}
byId("copy-broker-ip").addEventListener("click",async()=>{
  try {await navigator.clipboard.writeText(byId("broker-egress").textContent);byId("broker-notice").textContent="IP 주소를 복사했습니다.";}
  catch {byId("broker-notice").textContent="위 IP 주소를 길게 눌러 복사해주세요.";}
});
byId("connect-broker").addEventListener("click",async()=>{
  const button=byId("connect-broker");if(button.disabled)return;
  const data={client_id:byId("broker-client-id").value.trim(),client_secret:byId("broker-client-secret").value.trim(),account_seq:byId("broker-account-seq").value.trim()};
  if(!data.client_id || !data.client_secret){byId("broker-notice").textContent="Client ID와 Client Secret을 모두 입력해주세요.";return;}
  if(!data.account_seq)delete data.account_seq;
  button.disabled=true;clearBrokerKeys();byId("broker-notice").textContent="허용 IP와 계좌 접근을 확인하고 있습니다…";
  try {const pending=api("/v1/installation/broker",data,95000);data.client_id="";data.client_secret="";data.account_seq="";await pending;byId("broker-notice").textContent="연결 정보를 저장했습니다. 실시간 연결을 시작합니다.";await sync();}
  catch(error){byId("broker-notice").textContent=error.message==="account_selection_required" ? "계좌가 여러 개입니다. 사용할 accountSeq와 키를 다시 입력해주세요." : error.message==="broker_already_configured" ? "이미 연결 정보가 저장되어 있습니다. 잠시 후 다시 확인해주세요." : "연결을 마치지 못했습니다. 허용 IP·API 사용 승인·입력한 키를 확인하고 다시 시도해주세요.";}
  finally {button.disabled=false;clearBrokerKeys();}
});

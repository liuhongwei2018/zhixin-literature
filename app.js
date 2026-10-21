"use strict";
const $ = id => document.getElementById(id);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
let data, journalReference = {jcr:[],xinrui:[]}, journalIndexes = {}, selected = "all", saved = new Set();
try { saved = new Set(JSON.parse(localStorage.getItem("zhixin-saved-v1") || "[]")); } catch (_) {}
const sourceNames = {pubmed:"PubMed", epmc:"Europe PMC", biorxiv:"bioRxiv", medrxiv:"medRxiv", arxiv:"arXiv", other:"公开来源"};
const parts = {topic_relevance:"主题", publication_recency:"日期", article_type_cues:"类型", metadata_availability:"元数据", application_priority:"应用", abstract_review_adjustment:"摘要核对调整"};
const hotParts = {relevance:"相关性 /40",recency:"新近性 /30",design_method:"设计与方法线索 /20",metadata:"元数据 /10"};
const stateNames = {complete:"已完成", failed:"待重试", running:"检索中", pending:"等待执行"};

function safeLink(url) {
  try { const u = new URL(url); return u.protocol === "https:" && ["pubmed.ncbi.nlm.nih.gov","doi.org","arxiv.org","europepmc.org"].includes(u.hostname) && !u.username && !u.password; } catch (_) { return false; }
}
function dated(iso) { return iso ? new Date(iso).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}) : "待更新"; }
function noteFor(key) { return data.highlights.find(x => x.key === key); }
function normalizedName(v) { return String(v || '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]/g,''); }
function indexJournals() {
  journalIndexes={};
  for (const group of ['jcr','xinrui']) {
    const ids=new Map(),names=new Map(); const add=(map,k,r)=>{if(!map.has(k))map.set(k,new Set());map.get(k).add(r);};
    for(const r of journalReference[group] || []) {
      for(const id of r.issns || []) add(ids,id.replace(/-/g,''),r);
      for(const name of [r.name,r.abbreviation].filter(Boolean)) add(names,normalizedName(name),r);
    }
    journalIndexes[group]={ids,names};
  }
}
function journalMatch(p, group) {
  const idx=journalIndexes[group]; if(!idx)return null;
  const byId=new Set(),byName=new Set();
  for(const id of p.issns || []) for(const r of idx.ids.get(String(id).replace(/[^0-9X]/gi,'').toUpperCase()) || []) byId.add(r);
  for(const name of [p.journal,p.journal_abbreviation].filter(Boolean)) for(const r of idx.names.get(normalizedName(name)) || []) byName.add(r);
  if(byId.size && byName.size && ![...byId].some(r=>byName.has(r)))return null;
  const matches=byId.size ? byId : byName;return matches.size===1?[...matches][0]:null;
}
function bibliography(p) {
  const box=el('div','bibliography'), authors=(p.authors || []).filter(Boolean);
  box.append(el('p','authors','作者：'+(authors.length ? authors.slice(0,6).join('；')+(authors.length>6?' 等':'') : '来源待补')));
  if (authors.length>6) { const all=el('details','author-detail'); all.append(el('summary','','全部作者（'+authors.length+'）'),el('p','',authors.join('；'))); box.append(all); }
  const jcr=p.kind==='journal'?journalMatch(p,'jcr'):null, xr=p.kind==='journal'?journalMatch(p,'xinrui'):null;
  const missing=p.kind==='journal'?'未可靠匹配':'不适用（预印本）';
  box.append(el('p','journal-metrics','2025 影响因子：'+(jcr?.jif != null ? jcr.jif : missing)+' · 2026 新锐分区：'+(xr ? xr.partitions.map(x=>x.subject+' '+x.tier).join('；') : missing)));
  return box;
}
function toggleSave(key, button) {
  saved.has(key) ? saved.delete(key) : saved.add(key);
  try { localStorage.setItem("zhixin-saved-v1", JSON.stringify([...saved])); } catch (_) {}
  button.classList.toggle("saved", saved.has(key)); button.textContent = saved.has(key) ? "★" : "☆";
  button.setAttribute("aria-pressed", String(saved.has(key))); button.setAttribute("aria-label", saved.has(key) ? "取消收藏" : "收藏文章");
  if ($("type").value === "saved") render();
}
function article(p, r, direction) {
  const item = el("article", "paper"); item.append(el("span", "position", String($("sort").value === "hot" ? r.hot_position : r.position).padStart(2,"0")));
  const body = el("div", "paper-main"), meta = el("div", "meta");
  meta.append(el("span", "tag" + (p.kind === "preprint" ? " preprint" : ""), p.kind === "preprint" ? "预印本 · 未经同行评审" : "期刊文献"));
  meta.append(el("span", "", p.published || "发表日期待核"));
  meta.append(el("span", "", p.journal || sourceNames[p.source] || "公开来源"));
  if (r.abstract_classification_checked) meta.append(el("span", "", "摘要分类已核对"));
  body.append(meta);
  const title = el("a", "title", p.title); title.href = safeLink(p.url) ? p.url : "#"; title.target = "_blank"; title.rel = "noopener noreferrer"; body.append(title);
  body.append(bibliography(p));
  const note = noteFor(p.key);
  if (note) { body.append(el("p", "zh-title", note.short_title_zh)); body.append(el("p", "summary", note.summary_zh)); }
  const chips = el("div", "chips"); chips.append(el("span", "", r.article_type));
  for (const label of r.labels || []) chips.append(el("span", "", label));
  if (p.tracks.length > 1) chips.append(el("span", "", "跨方向收录"));
  if (p.version_updated && p.version_updated !== p.published) chips.append(el("span", "", "版本更新 " + p.version_updated));
  body.append(chips);
  const detail = el("details", "rank-detail"); detail.append(el("summary", "", "个人 HOT " + r.hot.score + " · 查看依据"));
  detail.append(el("p", "", Object.entries(r.hot.components || {}).map(([k,v]) => (hotParts[k] || k) + "：" + v).join(" · ")));
  const a = r.hot.attention, g = a.growth;
  let attention = a.status === "ok" ? "Europe PMC 被引 " + a.value + " 次；采集于 " + dated(a.observed_at) : a.status === "stale" ? "引文记录已过期，本轮不参与辅助排序。" : "引文暂不可用，不按零次处理。";
  attention += g.status === "ok" ? " 两次采集间隔 " + g.actual_hours + " 小时，来源计数增加 " + g.delta + "；按日折算 " + g.per_day + "。" : g.status === "counter_correction" ? " 来源计数回调，本轮不计算增长。" : " 尚无两次间隔至少 12 小时的有效记录，增长率留空。";
  detail.append(el("p", "", attention));
  detail.append(el("p", "", "按" + direction + "方向内排序。设计与方法为题录和摘要线索，尚未经全文质量评价；HOT 是个人阅读优先级。"));
  if (r.hot.recent_version) detail.append(el("p", "", "较早预印本的近期版本更新，不作为全新论文。")); body.append(detail);
  item.append(body);
  const button = el("button", "save" + (saved.has(p.key) ? " saved" : ""), saved.has(p.key) ? "★" : "☆"); button.type = "button"; button.setAttribute("aria-label", saved.has(p.key) ? "取消收藏" : "收藏文章"); button.setAttribute("aria-pressed", String(saved.has(p.key))); button.addEventListener("click", () => toggleSave(p.key, button)); item.append(button);
  return item;
}
function render() {
  const q = $("search").value.trim().toLocaleLowerCase(), type = $("type").value;
  $("heading").textContent = selected === "all" ? "四方向阅读清单" : data.tracks[selected].name;
  const fragment = document.createDocumentFragment(); let count = 0; const unique = new Set();
  for (const [key, t] of Object.entries(data.tracks)) {
    if (selected !== "all" && selected !== key) continue;
    let rows = t.reading.map(id => ({p:data.papers[id], r:t.rankings[id]})).filter(({p,r}) => {
      if (!$("preprints").checked && p.kind === "preprint") return false;
      if (type === "method" && !r.method_cue || type === "application" && r.method_cue || type === "reviewed" && !r.abstract_classification_checked || type === "saved" && !saved.has(p.key)) return false;
      const note = noteFor(p.key);
      return !q || [p.title,p.journal,...(p.authors || []),r.article_type,...r.labels,note?.short_title_zh,note?.summary_zh].filter(Boolean).join(" ").toLocaleLowerCase().includes(q);
    });
    if ($("sort").value === "date") rows.sort((a,b) => (b.p.published || "").localeCompare(a.p.published || "") || a.r.position-b.r.position);
    if ($("sort").value === "reading") rows.sort((a,b) => a.r.position-b.r.position);
    if (!rows.length) continue;
    const section = el("section", "track-section"), h = el("h3", "track-heading"); h.append(el("span", "", t.name), el("small", "", "目录候选 " + t.candidate_count.toLocaleString() + " · HOT 合格候选 " + t.readable_count.toLocaleString())); section.append(h);
    for (const row of rows) { section.append(article(row.p,row.r,t.name)); count++; unique.add(row.p.key); }
    fragment.append(section);
  }
  if (!count) fragment.append(el("p", "empty", "当前筛选下暂无条目，可以换个关键词或筛选条件。"));
  $("list").replaceChildren(fragment); $("result-count").textContent = count + " 条阅读条目 / " + unique.size + " 篇文献";
}
function nav() {
  const all = [["all", {name:"全部方向",reading:Object.keys(data.papers)}], ...Object.entries(data.tracks)];
  for (const [i,[key,t]] of all.entries()) {
    const b = el("button", key === selected ? "active" : ""); b.type = "button"; b.setAttribute("aria-pressed",String(key === selected)); b.dataset.track = key;
    b.append(el("span", "nav-index", i ? "0"+i : "◎"),el("span", "", t.name),el("span", "count", String(t.reading.length)));
    b.addEventListener("click", () => { selected = key; for (const n of $("topics").children) { n.classList.toggle("active",n === b); n.setAttribute("aria-pressed",String(n === b)); } render(); }); $("topics").append(b);
  }
}
async function init() {
  try {
    const response = await fetch("readings.json", {cache:"no-store"}); if (!response.ok) throw new Error("readings unavailable"); data = await response.json();
    try { const journals=await fetch('journal_metrics.json',{cache:'no-store'}); if(journals.ok) journalReference=await journals.json(); } catch (_) {}
    indexJournals();
    if (data.schema_version !== 2 || !data.tracks || !data.papers || !data.formal_hot_enabled || data.push_enabled) throw new Error("unsupported public reading data");
    $("paper-count").textContent = data.displayed_unique_papers + " 篇";
    $("coverage-count").textContent = data.coverage.complete + " / " + data.coverage.total;
    $("updated").textContent = dated(data.source_as_of);
    const age = Date.now()-new Date(data.source_as_of).getTime(), stale = age > 36*3600*1000;
    $("notice").classList.toggle("ok",data.all_due_sources_complete && !stale);
    $("notice").textContent = (stale ? "这份清单已超过 36 小时未更新。" : "") + (data.cloud_scanner_enabled ? "云端自动同步已接通，每 12 小时运行。" : "云端首次同步待验证。") + (data.all_due_sources_complete ? " 本轮 22 个来源任务已完成。" : " 本轮 " + data.coverage.complete + "/22 个来源任务完成；未完成窗口保留补扫，不代表没有新文献。") + " 个人 HOT 试运行 · 文献推送关闭。";
    $("ranking-description").textContent = data.ranking_description; $("update-description").textContent = data.update_description;
    const screening = data.public_screening;
    $("screening-description").textContent = screening ? screening.description + " 本次排除 " + screening.excluded_reading_entries + " 条已核实误命中，合并 " + screening.version_entries_merged + " 条旧版本重复展示。" : "";
    for (const j of data.coverage.jobs) { const row = el("tr"); row.append(el("td", "", j.track === "ALL" ? "四方向共用" : data.tracks[j.track].name),el("td", "", j.name),el("td", "", (j.start || "待核") + " → " + (j.through || "待核")),el("td", "state-"+j.status,stateNames[j.status] || "未知")); $("coverage-rows").append(row); }
    nav(); render(); for (const id of ["type","sort","preprints"]) $(id).addEventListener("change", render); $("search").addEventListener("input", render);
  } catch (_) { $("notice").textContent = "文献清单暂时无法读取，请稍后刷新。项目仓库保留最近一次公开数据。"; }
}
init();

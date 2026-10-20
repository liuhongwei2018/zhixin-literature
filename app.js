"use strict";
const $ = id => document.getElementById(id);
const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
let data, selected = "all", saved = new Set();
try { saved = new Set(JSON.parse(localStorage.getItem("zhixin-saved-v1") || "[]")); } catch (_) {}
const sourceNames = {pubmed:"PubMed", epmc:"Europe PMC", biorxiv:"bioRxiv", medrxiv:"medRxiv", arxiv:"arXiv", other:"公开来源"};
const parts = {topic_relevance:"主题", publication_recency:"日期", article_type_cues:"类型", metadata_availability:"元数据", application_priority:"应用", abstract_review_adjustment:"摘要核对调整"};
const stateNames = {complete:"已完成", failed:"待重试", running:"检索中", pending:"等待执行"};

function safeLink(url) {
  try { const u = new URL(url); return u.protocol === "https:" && ["pubmed.ncbi.nlm.nih.gov","doi.org","arxiv.org","europepmc.org"].includes(u.hostname) && !u.username && !u.password; } catch (_) { return false; }
}
function dated(iso) { return iso ? new Date(iso).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}) : "待更新"; }
function noteFor(key) { return data.highlights.find(x => x.key === key); }
function toggleSave(key, button) {
  saved.has(key) ? saved.delete(key) : saved.add(key);
  try { localStorage.setItem("zhixin-saved-v1", JSON.stringify([...saved])); } catch (_) {}
  button.classList.toggle("saved", saved.has(key)); button.textContent = saved.has(key) ? "★" : "☆";
  button.setAttribute("aria-pressed", String(saved.has(key))); button.setAttribute("aria-label", saved.has(key) ? "取消收藏" : "收藏文章");
  if ($("type").value === "saved") render();
}
function article(p, r, direction) {
  const item = el("article", "paper"); item.append(el("span", "position", String(r.position).padStart(2,"0")));
  const body = el("div", "paper-main"), meta = el("div", "meta");
  meta.append(el("span", "tag" + (p.kind === "preprint" ? " preprint" : ""), p.kind === "preprint" ? "预印本 · 未经同行评审" : "期刊文献"));
  meta.append(el("span", "", p.published || "发表日期待核"));
  meta.append(el("span", "", p.journal || sourceNames[p.source] || "公开来源"));
  if (r.abstract_classification_checked) meta.append(el("span", "", "摘要分类已核对"));
  body.append(meta);
  const title = el("a", "title", p.title); title.href = safeLink(p.url) ? p.url : "#"; title.target = "_blank"; title.rel = "noopener noreferrer"; body.append(title);
  const note = noteFor(p.key);
  if (note) { body.append(el("p", "zh-title", note.short_title_zh)); body.append(el("p", "summary", note.summary_zh)); }
  const chips = el("div", "chips"); chips.append(el("span", "", r.article_type));
  for (const label of r.labels || []) chips.append(el("span", "", label));
  if (p.tracks.length > 1) chips.append(el("span", "", "跨方向收录"));
  if (p.version_updated && p.version_updated !== p.published) chips.append(el("span", "", "版本更新 " + p.version_updated));
  body.append(chips);
  const detail = el("details", "rank-detail"); detail.append(el("summary", "", "阅读优先级 " + r.score + " · 查看依据"));
  detail.append(el("p", "", Object.entries(r.score_components || {}).map(([k,v]) => (parts[k] || k) + " " + v).join(" · ")));
  detail.append(el("p", "", "按" + direction + "方向内排序。分数为规则生成的阅读线索，尚未经全文质量评价。")); body.append(detail);
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
      return !q || [p.title,p.journal,r.article_type,...r.labels,note?.short_title_zh,note?.summary_zh].filter(Boolean).join(" ").toLocaleLowerCase().includes(q);
    });
    if ($("sort").value === "date") rows.sort((a,b) => (b.p.published || "").localeCompare(a.p.published || "") || a.r.position-b.r.position);
    if (!rows.length) continue;
    const section = el("section", "track-section"), h = el("h3", "track-heading"); h.append(el("span", "", t.name), el("small", "", "候选 " + t.candidate_count.toLocaleString() + " · 核心阅读候选 " + t.readable_count.toLocaleString())); section.append(h);
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
    if (data.schema_version !== 1 || !data.tracks || !data.papers || data.formal_hot_enabled || data.push_enabled) throw new Error("unsupported public reading data");
    $("paper-count").textContent = data.displayed_unique_papers + " 篇";
    $("coverage-count").textContent = data.coverage.complete + " / " + data.coverage.total;
    $("updated").textContent = dated(data.source_as_of);
    const age = Date.now()-new Date(data.source_as_of).getTime(), stale = age > 36*3600*1000;
    $("notice").classList.toggle("ok",data.all_due_sources_complete && !stale);
    $("notice").textContent = (stale ? "这份清单已超过 36 小时未更新。" : "") + (data.all_due_sources_complete ? "本轮检索窗口已完成。" : "本轮仍有 " + (data.coverage.total-data.coverage.complete) + " 个检索任务未完成，清单持续补全。") + " 当前显示公开阅读清单快照，正式 HOT 尚未启用。";
    $("ranking-description").textContent = data.ranking_description; $("update-description").textContent = data.update_description;
    const screening = data.public_screening;
    $("screening-description").textContent = screening ? screening.description + " 本次排除 " + screening.excluded_reading_entries + " 条已核实误命中，合并 " + screening.version_entries_merged + " 条旧版本重复展示。" : "";
    for (const j of data.coverage.jobs) { const row = el("tr"); row.append(el("td", "", j.track === "ALL" ? "四方向共用" : data.tracks[j.track].name),el("td", "", j.name),el("td", "", (j.start || "待核") + " → " + (j.through || "待核")),el("td", "state-"+j.status,stateNames[j.status] || "未知")); $("coverage-rows").append(row); }
    nav(); render(); for (const id of ["type","sort","preprints"]) $(id).addEventListener("change", render); $("search").addEventListener("input", render);
  } catch (_) { $("notice").textContent = "文献清单暂时无法读取，请稍后刷新。项目仓库保留最近一次公开数据。"; }
}
init();

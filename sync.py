"""Twice-daily durable scan, public catalogue and explainable personal HOT."""
import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import catalogue
import collect_sources
import hot
import metadata
import topics

ROOT = Path(__file__).resolve().parent
SCHEDULE = {"timezone": "Asia/Shanghai", "local_times": ["08:17", "20:17"], "interval_hours": 12,
            "cron_utc": "17 0,12 * * *", "timing": "GitHub schedule may be delayed; missed intervals are recovered from persisted source-day checkpoints."}
LANE_NAMES = {"pubmed_new": "PubMed · 新收录", "pubmed_updates": "PubMed · 题录更新",
              "epmc_new": "Europe PMC · 新收录", "epmc_updates": "Europe PMC · 题录更新",
              "arxiv": "arXiv · 新稿与版本", "biorxiv": "bioRxiv · 新稿与版本", "medrxiv": "medRxiv · 新稿与版本"}


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else copy.deepcopy(default)


def save(path, data):
    raw = catalogue.validate_public(data)
    temp = path.with_name(path.name+".next")
    temp.write_text(raw+"\n", encoding="utf-8")
    temp.replace(path)


def initial_state(now):
    return {"schema_version": 2, "baseline_start": (now.date()-timedelta(days=6)).isoformat(),
            "lanes": {}, "cooldowns": {}, "schedule": SCHEDULE, "history": []}


def lane_definitions():
    out = []
    for track in topics.NAMES:
        for source, lane in (("pubmed", "new"), ("pubmed", "updates"), ("epmc", "new"), ("epmc", "updates"), ("arxiv", "versions")):
            name = source+"_"+lane if source != "arxiv" else "arxiv"
            out.append((track+":"+name, track, source, lane, name))
    for source in ("biorxiv", "medrxiv"): out.append(("ALL:"+source, "ALL", source, "versions", source))
    return out


def daily_plan(state, lane_state, now):
    today = now.date()
    base = date.fromisoformat(state["baseline_start"])
    if lane_state.get("closed_through"):
        base = max(base, date.fromisoformat(lane_state["closed_through"])-timedelta(days=6))
    overlap = today-timedelta(days=6)
    if now.weekday() == 0:
        base = min(base, today-timedelta(days=27))
    records = lane_state.setdefault("days", {})
    days, day = [], base
    while day <= today:
        key = day.isoformat(); old = records.get(key, {})
        # Unfinished days remain due after the seven-day overlap moves forward.
        if old.get("status") != "complete" or day >= overlap or now.weekday() == 0 and day >= today-timedelta(days=27):
            days.append(key)
        day += timedelta(days=1)
    # Current data first; then recover the oldest gap. No gap is silently skipped.
    days = set(days) | {d for d, info in records.items() if "/" not in d and d <= today.isoformat() and info.get("status") != "complete"}
    return sorted(days, key=lambda d: (0 if d >= (today-timedelta(days=1)).isoformat() else 1, d))


def recovery_plan(state, now):
    last = state.get("last_run", {})
    try: fresh = 0 <= (now-hot.utc(last["finished_at"])).total_seconds() <= 6*3600
    except (ValueError, TypeError, KeyError): fresh = False
    enabled = last.get("status") == "partial" and fresh
    reusable = set()
    if enabled:
        for key, lane in state.get("lanes", {}).items():
            try: age = (now-hot.utc(lane["last_complete_at"])).total_seconds()
            except (ValueError, TypeError, KeyError): continue
            if lane.get("run_status") == "complete" and 0 <= age <= 6*3600: reusable.add(key)
    return enabled, reusable


def ingest(cat, rows, track, profiles, now, run_id):
    before = copy.deepcopy(cat.data)
    accepted = 0
    try:
        for p in rows:
            keys = list(profiles) if track == "ALL" else [track]
            ranks = {}
            body = " ".join([p.get("title", ""), p.get("abstract", ""), " ".join(p.get("keywords", []))])
            for key in keys:
                # Full preprint feeds are shared; discard only records outside all four queries.
                if track == "ALL" and not topics.evaluate(profiles[key]["ast"], body): continue
                ranks[key] = topics.classify(p, key, profiles[key], now)
            if ranks:
                # Update queries return many old papers. Screen all of them, but keep
                # the HOT catalogue bounded to eligible papers and tracked identities.
                # A known paper is always updated, including retractions/downgrades.
                existing = any(a in cat.index for a in catalogue.aliases(p))
                if not existing and not any(r["eligible"] for r in ranks.values()): continue
                cat.update(p, ranks, datetime.now(timezone.utc).isoformat(), run_id)
                accepted += 1
        cat.bound(now)
    except Exception:
        cat.data = before; cat.reindex(); raise
    return accepted


def public_report(cat, state, now, previous=None):
    doc = {"schema_version": 2, "as_of": now.isoformat(),
           "source_as_of": state.get("last_data_success_at") or (previous or {}).get("source_as_of"),
           "formal_hot_enabled": True, "push_enabled": False,
           "cloud_scanner_enabled": bool(state.get("cloud_execution_verified")),
           "scheduled_public_sync_enabled": bool(state.get("cloud_execution_verified")),
           "schedule": SCHEDULE, "hot_version": hot.VERSION,
           "ranking_description": "个人 HOT 试运行：主题与应用相关性 40 分、新近性 30 分、研究设计或方法线索 20 分、元数据完整度 10 分。引文仅辅助同类、两分区间内的近似同分排序；缺失不记零。分数不代表论文质量或全网热度。",
           "update_description": "GitHub 云端每 12 小时检索和发布，计划北京时间 08:17、20:17；可能因平台排队延迟。滚动重扫最近 7 天，周一回扫 28 天，失败日期保留补扫。文献推送关闭。",
           "papers": {}, "tracks": {}, "highlights": [], "coverage": {"jobs": [], "total": 22, "complete": 0},
           "public_screening": {"description": "稳定标识符合并；分类使用题名、摘要及来源类型，公开文件仅保留元数据和分类线索，不含摘要全文。", "excluded_reading_entries": 0, "version_entries_merged": 0},
           "catalogue_unique_papers": len(cat.data["papers"]), "last_run": state.get("last_run"),
           "attention_refresh": state.get("attention_refresh", {"status": "not_yet_observed"})}
    exclusions = {(e["track"], e["key"]) for e in cat.rules.get("exclusions", [])}
    for track, name in topics.NAMES.items():
        candidates, rows = 0, []
        for key, paper in cat.data["papers"].items():
            r = paper["rankings"].get(track)
            if not r: continue
            candidates += 1
            if any((track, a) in exclusions for a in paper["aliases"]): continue
            rank = hot.score(paper, r, now)
            if not rank["eligible"]: continue
            age = (now.date()-date.fromisoformat(paper["published"])).days
            rows.append({"key": key, "kind": paper["kind"], "hot": rank, "age_days": age, "rank": r})
        ordered = hot.order(rows)
        chosen = ordered[:100]
        # Preserve visibility of the requested omics subfields without changing HOT scores.
        if track == "omics_methods_applications":
            for label in ("CUT&Tag / CUT&RUN", "ATAC", "GWAS", "单细胞 / 单核", "空间组学", "RNA / 转录组"):
                if not any(label in x["rank"].get("labels", []) for x in chosen):
                    row = next((x for x in ordered if label in x["rank"].get("labels", [])), None)
                    if row: chosen.append(row)
        read_order = sorted(chosen, key=lambda x: (-x["rank"]["score"], x["key"]))
        read_positions = {x["key"]: i+1 for i, x in enumerate(read_order)}
        ranks = {}
        for row in chosen:
            key = row["key"]; paper = cat.data["papers"][key]
            if key not in doc["papers"]:
                doc["papers"][key] = {field: paper.get(field) for field in ("key", "url", "title", "journal", "authors", "issns", "journal_abbreviation", "published", "version_updated", "first_seen", "source", "sources", "kind")}
                doc["papers"][key]["tracks"] = []
            doc["papers"][key]["tracks"].append(track)
            r = row["rank"]
            ranks[key] = {**{field: r.get(field) for field in ("score", "score_components", "labels", "article_type", "method_cue", "abstract_classification_checked")},
                          "position": read_positions[key], "hot_position": next(i+1 for i, x in enumerate(ordered) if x["key"] == key), "hot": row["hot"]}
        doc["tracks"][track] = {"name": name, "candidate_count": candidates, "readable_count": len(rows),
                                "reading": [x["key"] for x in chosen], "rankings": ranks}
    for key, track, source, lane, name in lane_definitions():
        lane_state = state.get("lanes", {}).get(key, {})
        job = {"track": track, "name": LANE_NAMES[name], "source": source,
               "start": lane_state.get("window_start") or state["baseline_start"], "through": now.date().isoformat(),
               "status": lane_state.get("run_status", "pending"), "reason": lane_state.get("reason"),
               "last_success_at": lane_state.get("last_success_at"), "unfinished_days": lane_state.get("unfinished_days", [])}
        doc["coverage"]["jobs"].append(job)
    doc["coverage"]["complete"] = sum(j["status"] == "complete" for j in doc["coverage"]["jobs"])
    doc["all_due_sources_complete"] = doc["coverage"]["complete"] == 22
    doc["displayed_unique_papers"] = len(doc["papers"])
    return doc


def refresh_attention(cat, client, now, run_id, report):
    targets = list(report["papers"])
    success, failed = 0, 0
    for start in range(0, len(targets), 25):
        group = targets[start:start+25]
        terms = []
        for key in group:
            paper = cat.data["papers"][key]
            pmid = next((a[5:] for a in paper["aliases"] if a.startswith("pmid:")), None)
            doi = next((a[4:] for a in paper["aliases"] if a.startswith("doi:")), None)
            if pmid: terms.append("(SRC:MED AND EXT_ID:"+pmid+")")
            elif doi and '"' not in doi: terms.append('DOI:"'+doi+'"')
        if not terms: continue
        try:
            payload = client.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", {"query": " OR ".join(terms), "format": "json", "resultType": "core", "pageSize": 1000})
            rows = payload["resultList"]["result"]
            if int(payload["hitCount"]) != len(rows): raise collect_sources.SourceFailure("attention_page_truncated")
            for row in rows:
                p = metadata.epmc_row(row)
                existing = {cat.index[a] for a in catalogue.aliases(p) if a in cat.index}
                if len(existing) == 1:
                    # Metric-only refresh must not change classification or bibliographic authority.
                    old = cat.data["papers"][next(iter(existing))]
                    if type(p.get("citations")) is int and p["citations"] >= 0:
                        m = {"source": "Europe PMC", "metric": "citedByCount", "entity": p["id"], "lineage": "epmc-citedByCount-v1",
                             "status": "ok", "value": p["citations"], "observed_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id}
                        old["metrics"] = [v for v in old.get("metrics", []) if not (v["entity"] == m["entity"] and v.get("run_id") == run_id)]+[m]
                        grouped = {}
                        for v in old["metrics"]: grouped.setdefault((v["source"], v["entity"], v["metric"]), []).append(v)
                        old["metrics"] = [v for values in grouped.values() for v in sorted(values, key=lambda v: v["observed_at"])[-8:]]
                        success += 1
        except collect_sources.BudgetExpired: raise
        except (collect_sources.SourceFailure, KeyError, TypeError, ValueError): failed += 1
    return {"status": "complete" if failed == 0 else "partial", "observed_records": success,
            "failed_batches": failed, "as_of": datetime.now(timezone.utc).isoformat(), "target_papers": len(targets)}


def run(root, budget_seconds=3300):
    started = datetime.now(timezone.utc)
    run_id = os.environ.get("GITHUB_RUN_ID", "manual-"+started.strftime("%Y%m%dT%H%M%SZ"))+"-"+os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    state = read(root/"scan_state.json", initial_state(started))
    state["schedule"] = SCHEDULE
    state["cloud_execution_verified"] = os.environ.get("GITHUB_ACTIONS") == "true"
    cat = catalogue.Catalogue(read(root/"catalog.json", catalogue.empty()), read(root/"publication_rules.json", {}))
    profiles = topics.profiles(root)
    previous = read(root/"readings.json", {})
    client = collect_sources.Client(state, budget_seconds)
    definitions = lane_definitions()
    recovering, reusable = recovery_plan(state, started)
    for key, track, source, lane, name in definitions:
        if key in reusable: continue
        ls = state["lanes"].setdefault(key, {})
        ls.update(run_status="pending", reason=None)
    # Unique ephemeral cache: never publish source responses or reuse stale partial pages across runs.
    run_parent = root/".run"; run_parent.mkdir(mode=0o700, exist_ok=True)
    cache = Path(tempfile.mkdtemp(prefix="scan-", dir=run_parent))
    completed, failed, source_records, budget = 0, 0, 0, False
    for key, track, source, lane, name in definitions:
        if key in reusable: continue
        ls = state["lanes"][key]
        if budget:
            ls["reason"] = "run_budget_reached"; continue
        ls["run_status"] = "running"
        if source in ("arxiv", "biorxiv", "medrxiv"):
            # One checked interval per supplemental feed; overlap and missed-window recovery.
            overlap = started.date()-timedelta(days=27 if started.weekday() == 0 else 6)
            previous_through = date.fromisoformat(ls.get("last_complete_through", state["baseline_start"]))
            start = min(overlap, previous_through).isoformat()
            if ls.get("failed_from"): start = min(start, ls["failed_from"])
            plans = [(start, started.date().isoformat())]
        else:
            days = daily_plan(state, ls, started)
            if recovering:
                def needs_retry(day):
                    info = ls.get("days", {}).get(day, {})
                    try: age = (started-hot.utc(info["checked_at"])).total_seconds()
                    except (ValueError, TypeError, KeyError): return True
                    return info.get("status") != "complete" or not 0 <= age <= 6*3600
                days = [day for day in days if needs_retry(day)]
            plans = [(day, day) for day in days]
        ls["window_start"] = min((p[0] for p in plans), default=started.date().isoformat())
        all_complete = True
        for start, end in plans:
            token = start if start == end else start+"/"+end
            try:
                rows, counts = collect_sources.collect(client, profiles.get(track), source, lane, start, end, cache)
                accepted = ingest(cat, rows, track, profiles, started, run_id)
                stamp = datetime.now(timezone.utc).isoformat()
                ls.setdefault("days", {})[token] = {"status": "complete", "checked_at": stamp, **counts, "accepted": accepted}
                ls["last_success_at"] = stamp
                state["last_data_success_at"] = stamp
                source_records += len(rows); completed += 1
                print(json.dumps({"source": source, "track": track, "lane": lane, "day": token, "status": "complete", "received": len(rows), "accepted": accepted}), flush=True)
            except collect_sources.BudgetExpired:
                budget = True; all_complete = False; ls["reason"] = "run_budget_reached"; break
            except Exception as error:
                reason = getattr(error, "reason", None) or getattr(error, "details", {}).get("reason") or type(error).__name__
                ls.setdefault("days", {})[token] = {"status": "failed", "checked_at": datetime.now(timezone.utc).isoformat(), "reason": reason}
                ls["reason"] = reason; failed += 1; all_complete = False
                print(json.dumps({"source": source, "track": track, "lane": lane, "day": token, "status": "failed", "reason": reason}), flush=True)
                # Do not hammer a failing host for each remaining day.
                break
        ls["run_status"] = "complete" if all_complete else "failed"
        if all_complete:
            ls["last_complete_at"] = datetime.now(timezone.utc).isoformat()
            if source in ("arxiv", "biorxiv", "medrxiv"):
                ls["last_complete_through"] = started.date().isoformat()
                ls.pop("failed_from", None)
        elif source in ("arxiv", "biorxiv", "medrxiv"):
            ls["failed_from"] = ls["window_start"]
        if source in ("pubmed", "epmc"):
            day = date.fromisoformat(ls.get("closed_through", state["baseline_start"]))
            while day < started.date():
                rec = ls.get("days", {}).get(day.isoformat(), {})
                if rec.get("status") != "complete" or rec.get("checked_at", "")[:10] <= day.isoformat(): break
                ls["closed_through"] = day.isoformat(); day += timedelta(days=1)
        ls["unfinished_days"] = [day for day, info in ls.get("days", {}).items() if info["status"] != "complete"]
        save(root/"catalog.json", cat.data)
        save(root/"scan_state.json", state)
    cat.bound(started)
    status = "complete" if all(ls.get("run_status") == "complete" for ls in state["lanes"].values()) else "partial"
    state["last_run"] = {"id": run_id, "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
                         "mode": "recovery" if recovering else "full_overlap", "reused_complete_lanes": len(reusable),
                         "status": status, "completed_partitions": completed, "failed_partitions": failed, "source_records": source_records,
                         "budget_reached": budget, "workflow_url": "https://github.com/"+os.environ.get("GITHUB_REPOSITORY", "liuhongwei2018/zhixin-literature")+"/actions/runs/"+os.environ.get("GITHUB_RUN_ID", "")}
    if not budget:
        try: state["attention_refresh"] = refresh_attention(cat, client, started, run_id, public_report(cat, state, started, previous))
        except collect_sources.BudgetExpired: state["attention_refresh"] = {"status": "budget_reached"}
    else: state["attention_refresh"] = {"status": "budget_reached"}
    state["last_run"]["http_requests"] = client.requests
    state["last_run"]["finished_at"] = datetime.now(timezone.utc).isoformat()
    state["history"] = (state.get("history", [])+[state["last_run"]])[-28:]
    # Keep aggregate daily checkpoints for 120 days; never drop failed gaps.
    cutoff = (started.date()-timedelta(days=120)).isoformat()
    for ls in state["lanes"].values():
        ls["days"] = {k:v for k,v in ls.get("days", {}).items() if k >= cutoff or v.get("status") != "complete" or k >= ls.get("closed_through", "")}
    finished = datetime.now(timezone.utc)
    report = public_report(cat, state, finished, previous)
    weekly = {"schema_version": 1, "as_of": finished.isoformat(), "window_start": (finished.date()-timedelta(days=6)).isoformat(),
              "mode": "rolling_seven_day_digest_no_push", "tracks": {k: {"name": t["name"], "papers": t["reading"][:20]} for k,t in report["tracks"].items()}}
    for name, value in (("catalog.json", cat.data), ("scan_state.json", state), ("readings.json", report), ("weekly.json", weekly)):
        save(root/name, value)
    return 0 if status == "complete" else 2


def validate(root):
    for name in ("catalog.json", "scan_state.json", "readings.json", "weekly.json", "queries.json", "publication_rules.json", "journal_metrics.json"):
        catalogue.validate_public(read(root/name))
    doc = read(root/"readings.json")
    if doc["schema_version"] != 2 or not doc["formal_hot_enabled"] or doc["push_enabled"]: raise ValueError("invalid_feature_flags")
    if set(doc["tracks"]) != set(topics.NAMES): raise ValueError("invalid_public_tracks")
    if doc["displayed_unique_papers"] != len(doc["papers"]): raise ValueError("public_unique_count_mismatch")
    for track, t in doc["tracks"].items():
        if len(t["reading"]) != len(set(t["reading"])): raise ValueError("duplicate_public_reading")
        for key in t["reading"]:
            p = doc["papers"][key]; r = t["rankings"][key]
            if not r["hot"]["eligible"] or track not in p["tracks"]: raise ValueError("invalid_hot_entry")
            if not 0 <= r["hot"]["score"] <= 100: raise ValueError("invalid_hot_score")
            if abs(sum(r["hot"]["components"].values())-r["hot"]["score"]) > .011: raise ValueError("hot_component_mismatch")
    return doc


def build_site(root, target):
    validate(root)
    if target.exists(): raise ValueError("site_target_already_exists")
    target.mkdir(mode=0o700)
    for name in ("index.html", "app.js", "style.css", "favicon.svg", "readings.json", "weekly.json", "journal_metrics.json"):
        shutil.copyfile(root/name, target/name)
    (target/".nojekyll").write_text("")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget-seconds", type=int, default=3300)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--build-site", type=Path)
    args = parser.parse_args()
    if args.build_site: build_site(ROOT, args.build_site)
    elif args.validate: validate(ROOT)
    else: sys.exit(run(ROOT, args.budget_seconds))

"""Personal HOT v1: explainable reading priority, with separate observed attention."""
from datetime import datetime, timezone, date
import math
import re

VERSION = "personal-hot-v1-20261020"
WEIGHTS = {"relevance": 40, "recency": 30, "design_method": 20, "metadata": 10}


def utc(value):
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return d.astimezone(timezone.utc)


def signals(p, r):
    text = " ".join([p.get("title", ""), p.get("abstract", ""), " ".join(p.get("publication_types", []))]).lower()
    sentences = re.split(r"[.!?;\n]+", text)
    def has(words):
        for sentence in sentences:
            if not any(w in sentence for w in words): continue
            # A limitation or future intention is not evidence of completed validation.
            if re.search(r"\b(?:need(?:s|ed)?|requir(?:e|es|ed)|future|should|warrant(?:s|ed)?|lack(?:s|ed|ing)?|without|not|no|will|planned)\b", sentence): continue
            return True
        return False
    return {
        "abstract_available": bool(p.get("abstract") or p.get("abstract_excerpt")),
        "benchmark": has(["benchmark", "comparative evaluation", "comparison of methods"]),
        "external_validation": has(["external validation", "external cohort", "independent validation", "independent cohort"]),
        "multicenter": has(["multicenter", "multi-center", "multicentre", "multi-centre", "nationwide", "population-based"]),
        "prospective": has(["prospective", "longitudinal", "follow-up"]),
        "comparison": has(["target trial", "comparative effectiveness", "randomized", "randomised", "propensity"]),
        "replication": has(["replicat", "independent dataset", "independent data set"]),
        "code_available": has(["github.com/", "code is available", "open-source", "open source"]),
        "method": bool(r.get("method_cue")),
    }


def growth(history, latest, now):
    result = {"status": "insufficient_12h_history", "delta": None, "per_day": None, "actual_hours": None}
    if latest.get("status") != "ok" or type(latest.get("value")) is not int:
        return {**result, "status": latest.get("status", "unavailable")}
    try: current = utc(latest["observed_at"])
    except (ValueError, TypeError, KeyError): return {**result, "status": "invalid_observation_time"}
    if current > now or (now-current).total_seconds() > 36*3600:
        return {**result, "status": "stale_or_future_observation"}
    candidates = []
    for p in history:
        if any(p.get(k) != latest.get(k) for k in ("source", "entity", "metric", "lineage")):
            continue
        if p.get("status") != "ok" or type(p.get("value")) is not int or p["value"] < 0:
            continue
        try:
            hours = (current-utc(p["observed_at"])).total_seconds()/3600
        except (ValueError, TypeError, KeyError):
            continue
        if 12 <= hours <= 36:
            candidates.append((hours, p))
    if not candidates:
        return result
    hours, before = min(candidates, key=lambda x: abs(x[0]-12))
    delta = latest["value"]-before["value"]
    if delta < 0:
        return {**result, "status": "counter_correction", "actual_hours": round(hours, 3)}
    return {"status": "ok", "delta": delta, "per_day": round(delta*24/hours, 4),
            "actual_hours": round(hours, 3), "meaning": "change observed in one source counter; not exact citation event dates"}


def attention(p, now):
    history = p.get("metrics", [])
    valid = [x for x in history if x.get("source") == "Europe PMC" and x.get("metric") == "citedByCount"
             and x.get("status") == "ok" and type(x.get("value")) is int and x["value"] >= 0]
    def dated(x):
        try: utc(x.get("observed_at")); return True
        except (ValueError, TypeError): return False
    valid = [x for x in valid if dated(x)]
    if not valid:
        return {"status": "unavailable", "value": None, "source": "Europe PMC", "growth": {"status": "insufficient_12h_history", "delta": None, "per_day": None}}
    # MED, PMC and preprint counters are different lineages; never sum or splice them.
    preferred = "MED:" if p.get("kind") == "journal" else "PPR:"
    valid.sort(key=lambda x: (x.get("entity", "").startswith(preferred), x["observed_at"]))
    latest = valid[-1]
    age = (now-utc(latest["observed_at"])).total_seconds()
    status = "ok" if 0 <= age <= 36*3600 else "stale"
    return {"status": status, "value": latest["value"], "source": latest["source"],
            "entity": latest["entity"], "observed_at": latest["observed_at"],
            "growth": growth(history, latest, now)}


def score(p, rank, now=None):
    now = now or datetime.now(timezone.utc)
    pub = None
    try:
        pub = date.fromisoformat(str(p.get("published") or rank.get("published")))
    except (ValueError, TypeError):
        pass
    age = (now.date()-pub).days if pub else None
    updated_age = None
    try:
        updated_age = (now.date()-date.fromisoformat(str(p.get("version_updated") or "")[:10])).days
    except (ValueError, TypeError):
        pass
    recent_version = p.get("kind") == "preprint" and updated_age is not None and 0 <= updated_age <= 14
    eligible = bool(rank.get("eligible") and not p.get("retracted") and not p.get("identity_review")
                    and age is not None and age >= 0 and (age <= 60 or recent_version))
    topic = rank.get("topic_strength", rank.get("score_components", {}).get("topic_relevance", 0))
    relevance = round(40*min(55, max(0, float(topic)))/55, 2)
    recency = round(30*math.exp(-age/14), 2) if age is not None and age >= 0 else 0
    if recent_version and age is not None and age > 14:
        recency = max(recency, round(min(16, 16*math.exp(-updated_age/14)), 2))
    features = rank.get("signals") or signals(p, rank)
    article_type = rank.get("article_type", "")
    if any(t in article_type for t in ("评论", "通信", "解读")):
        design = 4
    elif "方案" in article_type or "设计" in article_type:
        design = 2
    elif "综述" in article_type or "指南" in article_type or "共识" in article_type:
        design = 6
    elif features.get("method"):
        design = 10 + 5*features.get("benchmark", False) + 3*features.get("external_validation", False) + 2*features.get("code_available", False)
    else:
        design = 8 + 5*features.get("external_validation", False) + 3*features.get("multicenter", False) + 2*features.get("prospective", False) + 2*features.get("comparison", False)
        design += 2*features.get("replication", False)
    design = min(20, design)
    metadata = 5*bool(features.get("abstract_available")) + 3*bool(pub) + 2*bool(p.get("aliases") or p.get("key"))
    components = {"relevance": relevance, "recency": recency, "design_method": design, "metadata": metadata}
    return {"version": VERSION, "eligible": eligible, "score": round(sum(components.values()), 2),
            "components": components, "weights": WEIGHTS, "attention": attention(p, now),
            "methodology": "Personal reading priority, not scientific quality or a validated social-trend score. Citations are an auxiliary tie-breaker only; unavailable counters are never zero-filled.",
            "recent_version": bool(recent_version and age is not None and age > 14)}


def order(rows):
    """Use attention only between observed counters in the same 2-point/kind group.

    Missing-counter entries retain their base-score positions. A missing count
    therefore supplies neither a citation penalty nor an invented zero.
    """
    rows = sorted(rows, key=lambda x: (-x["hot"]["score"], x["key"]))
    groups = {}
    for i, row in enumerate(rows):
        a = row["hot"]["attention"]
        if a.get("status") == "ok":
            group = (int(row["hot"]["score"]//2), row.get("kind"))
            groups.setdefault(group, []).append(i)
    for indexes in groups.values():
        def velocity(row):
            try:
                age = max(7, row["age_days"])
            except (KeyError, TypeError):
                age = 7
            return (row["hot"]["attention"]["value"]/age, row["hot"]["score"])
        ranked = sorted((rows[i] for i in indexes), key=velocity, reverse=True)
        for i, row in zip(indexes, ranked):
            rows[i] = row
    return rows

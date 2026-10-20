"""Four editable retrieval profiles, shared local matching and reading priority."""
import functools, json, re, unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

NAMES = {
 "CHIP_mCA":"CHIP / mCA",
 "AI_medical_imaging":"AI 医学影像",
 "omics_methods_applications":"组学方法与应用",
 "clinical_observational":"病案与临床数据研究",
}

@functools.lru_cache(maxsize=1024)
def normalize(s):
    s=unicodedata.normalize("NFKC",str(s or "")).lower()
    return re.sub(r"[^a-z0-9*]+"," ",s).strip()

@functools.lru_cache(maxsize=2048)
def pattern(term):
    words=normalize(term).split()
    return re.compile(r"\b"+r"\s+".join(re.escape(w).replace(r"\*",r"[a-z0-9]*") for w in words)+r"\b")

def contains(text,term):return bool(pattern(term).search(normalize(text)))

def parse(query):
    rx=re.compile(r'\s*(?:("[^"]+"|[A-Za-z0-9*&.+-]+)\[Title/Abstract\]|(AND|OR|NOT)\b|([()]))')
    toks=[];pos=0
    while pos<len(query):
        if not query[pos:].strip():break
        m=rx.match(query,pos)
        if not m:raise ValueError("Unsupported PubMed syntax at "+str(pos))
        toks.append(("term",m[1].strip('"')) if m[1] else (m[2] or m[3],None));pos=m.end()
    cursor=0
    def atom():
        nonlocal cursor
        op,val=toks[cursor];cursor+=1
        if op=="term":return ("term",val)
        if op=="NOT":return ("NOT",atom())
        if op=="(":
            node=expr()
            if cursor>=len(toks) or toks[cursor][0]!=")":raise ValueError("Unbalanced query")
            cursor+=1;return node
        raise ValueError("Unexpected query token")
    def conjunction():
        nonlocal cursor
        node=atom()
        while cursor<len(toks) and toks[cursor][0]=="AND":
            cursor+=1;node=("AND",node,atom())
        return node
    def expr():
        nonlocal cursor
        node=conjunction()
        while cursor<len(toks) and toks[cursor][0]=="OR":
            cursor+=1;node=("OR",node,conjunction())
        return node
    node=expr()
    if cursor!=len(toks):raise ValueError("Trailing query tokens")
    return node

def evaluate(node,text):
    op=node[0]
    if op=="term":return contains(text,node[1])
    if op=="NOT":return not evaluate(node[1],text)
    return (evaluate(node[1],text) and evaluate(node[2],text)) if op=="AND" else (evaluate(node[1],text) or evaluate(node[2],text))

def epmc_query(node):
    op=node[0]
    if op=="term":
        term=node[1]
        if "*" in term and " " in term:
            return "("+" AND ".join("TITLE_ABS:"+w for w in normalize(term).split())+")"
        return "TITLE_ABS:"+('"'+term+'"' if re.search(r"[^a-zA-Z0-9*]",term) else term)
    if op=="NOT":return "NOT ("+epmc_query(node[1])+")"
    return "("+epmc_query(node[1])+" "+op+" "+epmc_query(node[2])+")"

def profiles(root):
    doc=json.loads((Path(root)/"queries.json").read_text());out={}
    for key,p in doc["profiles"].items():
        node=parse(p["pubmed_query"])
        out[key]={**p,"key":key,"name":NAMES[key],"ast":node,
                  "queries":{"pubmed":p["pubmed_query"],"epmc":epmc_query(node)}}
    return out

ARXIV_SEEDS={
 "CHIP_mCA":'(all:"clonal hematopoiesis" OR all:"clonal haematopoiesis" OR all:"mosaic chromosomal" OR all:"mosaic loss of Y")',
 "AI_medical_imaging":'((all:"deep learning" OR all:"machine learning" OR all:"artificial intelligence" OR all:"foundation model" OR all:"vision-language" OR all:transformer) AND (all:medical OR all:radiology OR all:histopathology OR all:"magnetic resonance" OR all:ultrasound OR all:"computed tomography")) OR all:radiomics OR all:radiomic',
 "omics_methods_applications":'(all:"RNA-seq" OR all:RNAseq OR all:transcriptomics OR all:transcriptome OR all:"single-cell" OR all:"single-nucleus" OR all:"CUT&Tag" OR all:"CUT&RUN" OR all:"ATAC-seq" OR all:GWAS OR all:"genome-wide association")',
 "clinical_observational":'((all:"electronic health record" OR all:"electronic medical record" OR all:"medical records" OR all:"UK Biobank" OR all:"MIMIC" OR all:"SEER" OR all:"real-world data") AND (all:risk OR all:survival OR all:mortality OR all:outcome OR all:association))',
}

CORE_CH=["clonal hematopoie*","clonal haematopoie*","mosaic chromosomal alter*","mosaic chromosomal abnormal*","mosaic loss of Y","mLOY","loss of Y chromosome","loss of chromosome Y"]
AI=["artificial intelligence","machine learning","deep learning","neural network*","foundation model*","vision language model*","large language model*","transformer*","diffusion model*","self supervised"]
IMAGE=["radiomic*","medical imag*","diagnostic imag*","computed tomography","magnetic resonance","MRI","ultrasound","mammograph*","radiograph*","echocardiogra*","optical coherence tomography","fundus","retinal imag*","digital pathology","whole slide","histopatholog*"]
DATA=["electronic health record*","electronic medical record*","medical record*","hospital record*","hospital discharge*","insurance claim*","claims data","administrative data","clinical database*","routinely collected","registry*","real world data","real world evidence","MIMIC","UK Biobank","SEER","National Cancer Database","Clinical Practice Research Datalink","NHANES"]
DESIGN=["cohort*","case control","observational","retrospective","longitudinal","population based","target trial emulation","follow up","multivariable regression","Cox regression","Cox proportional"]
OUTCOME=["risk*","incidence","prevalence","mortality","survival","prognos*","outcome*","association*","comparative effectiveness"]
METHOD=["method*","algorithm*","framework*","benchmark*","pipeline*","software","deconvolution","batch correction","fine mapping","colocalization"]
NEW_METHOD=["we introduce","we develop","we present","we propose","novel method","new method","new framework","benchmark","comparative evaluation"]
ANIMALS=["mouse","mice","murine","rat","rats","porcine","swine","pig","pigs","zebrafish","sheep","ovine","canine","bovine","veterinary","poultry","chicken"]
PLANTS=["plant","plants","cotton","arabidopsis","maize","wheat","rice","crop","crops","soybean"]

def hits(text,terms):return [t for t in terms if contains(text,t)]

def classify(p,key,profile,now=None):
    now=now or datetime.now(timezone.utc);title=p.get("title","");abstract=p.get("abstract","")
    body=" ".join([title,abstract," ".join(p.get("keywords",[]))])
    matched=evaluate(profile["ast"],body)
    labels=[];evidence={};core=matched;reason=[]
    if key=="CHIP_mCA":
        h=hits(body,CORE_CH);th=hits(title,CORE_CH);evidence["topic_terms"]=h
        core=bool(th or sum(len(pattern(t).findall(normalize(abstract))) for t in CORE_CH)>=2)
        if not h:
            core=False;reason.append("缩写命中，缺少明确克隆性造血或染色体嵌合语境")
        elif not core:reason.append("主题主要出现在背景，核心问题相关性待核")
        labels+=["mCA / Y染色体嵌合"] if hits(body,CORE_CH[2:]) else ["CHIP / 克隆性造血"]
    elif key=="AI_medical_imaging":
        ah=hits(body,AI);ih=hits(body,IMAGE);rh=hits(body,["radiomic*"])
        evidence.update(ai_terms=ah,imaging_terms=ih)
        core=bool((ah and ih) or rh)
        actual_image_cues=hits(body,["image*","imaging","radiomic*","MRI","magnetic resonance","computed tomography","echocardiogra*","optical coherence tomography","radiograph*","mammograph*","ultrasound","whole slide","fundus","retinal imag*","photograph*"])
        evidence["image_input_cues"]=actual_image_cues
        if not actual_image_cues:
            core=False;reason.append("仅提到病理或诊断检查，缺少实际影像输入线索，保留相关候选")
        if rh and not ah:reason.append("影像组学相关；AI方法尚未明确")
        if hits(body,["digital pathology","histopatholog*","whole slide"]):labels.append("病理影像")
        if hits(body,["fundus","optical coherence tomography","retinal imag*"]):labels.append("眼科影像")
        if hits(body,["computed tomography","magnetic resonance","MRI","radiograph*","ultrasound","radiomic*"]):labels.append("放射与超声影像")
        if not labels:labels.append("其他医学影像")
    elif key=="omics_methods_applications":
        for sub,q in profile.get("subqueries",{}).items():
            if evaluate(parse_cached(q),body):
                labels.append({"RNA_transcriptomics":"RNA / 转录组","single_cell_nucleus":"单细胞 / 单核","spatial_omics":"空间组学","CUT":"CUT&Tag / CUT&RUN","ATAC":"ATAC","GWAS":"GWAS"}[sub])
        evidence["subfields"]=labels[:];core=bool(labels)
    else:
        ds=hits(body,DATA);de=hits(body,DESIGN);ou=hits(body,OUTCOME)
        evidence.update(data_source_terms=ds,design_terms=de,outcome_terms=ou)
        core=bool(ou and (ds or (de and hits(body,["patient*","clinical","hospital*"]))))
        if ds:labels.append("病案 / 登记 / 数据库")
        if hits(body,["prognos*","prediction","predictive","model development"]):labels.append("临床预测")
        if hits(body,["association*","risk factor*","incidence","prevalence"]):labels.append("人群风险与关联")
        if hits(body,["comparative effectiveness","target trial","propensity score","treatment effect"]):labels.append("治疗效果比较")
        if hits(title,["biomarker*","RNA","transcriptom*","gene*"]) and not ds:
            core=False;reason.append("主要是实验或组学标志物研究，常规临床数据来源不足")
        if hits(body,["TCGA","the cancer genome atlas","Gene Expression Omnibus"]) and hits(body,["gene expression","transcriptom*","RNA"]) and not ds:
            core=False;labels.append("组学驱动的临床关联");reason.append("主要基于组学数据库的关联，保留相关候选，不占临床数据主榜")
        if hits(title,["case series","technical note"]) or (hits(body,["eleven patients","11 patients"]) and hits(title,["technique","repair*"])):
            core=False;reason.append("小病例系列或技术报告，保留相关候选")
    if key=="omics_methods_applications" and not hits(body,["DNA","RNA","gene*","genom*","transcript*","epigen*","chromatin","tissue*","biolog*","sequencing","cell type*","cellular*","patient*","genetic*"]):
        core=False;reason.append("缺少生物组学语境；single cell 等词可能指网格或器件")
    if not matched:
        core=False;reason.append("本地题名、摘要和关键词未复现主题语义，保留检索候选")
    ptypes=" ".join(p.get("publication_types",[]))+" "+title
    review=bool(hits(ptypes,["review","meta analysis","systematic review","bibliometric*"]))
    guideline=bool(hits(ptypes,["guideline","recommendations","consensus"]))
    commentary=bool(hits(" ".join(p.get("publication_types",[])),["editorial","comment"]) or hits(abstract,["in this issue","in the accompanying article"]))
    letter=bool(hits(" ".join(p.get("publication_types",[])),["letter"]))
    protocol=bool(hits(title,["study protocol","trial protocol","protocol paper","design of","rationale and design"]) or hits(abstract,["will be conducted","will be enrolled"]))
    method_cue=bool(hits(title,METHOD) and hits(abstract,NEW_METHOD))
    typ="评论 / 通信" if commentary else ("研究方案 / 设计" if protocol else ("综述 / 汇总" if review else ("指南 / 共识" if guideline else ("研究通信候选" if letter else ("方法 / 评测候选" if method_cue else "应用 / 原始研究候选")))))
    if key=="clinical_observational" and "临床预测" in labels and not (review or commentary or protocol):typ="临床预测研究候选"
    if protocol:reason.append("研究方案或设计线索，不能当作已完成的临床结果")
    plant=bool(hits(title,PLANTS))
    animal=bool(hits(title,ANIMALS))
    human=bool(hits(body,["patients","participants","human","healthy controls","clinical cohort","UK Biobank","SEER","MIMIC"]))
    context="plant_application" if plant else ("animal_application" if animal else ("human_clinical" if human else "unspecified_or_general_method"))
    evidence["application_context"]=context
    application_bonus=8 if human and not (plant or animal) else 0
    if key=="AI_medical_imaging" and (plant or animal):application_bonus=-30
    elif key=="omics_methods_applications" and not method_cue:
        application_bonus=-25 if plant else (-8 if animal else application_bonus)
    elif key=="clinical_observational":
        application_bonus+=10 if ds else 0
        if plant or animal:core=False;reason.append("非人体临床应用，保留相关候选")
    if application_bonus<0:reason.append("应用场景与人体临床关注点距离较远，降低阅读优先级")
    if not abstract:reason.append("摘要缺失，研究类型与结论待核")
    valid_dates=[];future_dates=[];raw_dates=p.get("publication_dates",[])
    if p.get("kind")=="journal":
        formal=[d for d in raw_dates if d.get("type") not in ("preprint_v1","arxiv_first_submission")]
        if formal:raw_dates=formal
    if not raw_dates and p.get("published"):raw_dates=[{"value":p["published"],"type":"source_publication"}]
    for d in raw_dates:
        v=d.get("value","")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}",v):continue
        try:pd=date.fromisoformat(v)
        except ValueError:continue
        (valid_dates if pd<=now.date() else future_dates).append(pd)
    pub=min(valid_dates) if valid_dates else None
    age=(now.date()-pub).days if pub else None
    updated=str(p.get("updated") or p.get("version_date") or "")[:10]
    recent_version=p.get("kind")=="preprint" and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}",updated)) and 0<=(now.date()-date.fromisoformat(updated)).days<=30
    date_state="verified_source_date" if pub else ("future_source_date" if future_dates else "insufficient_publication_date")
    recent=bool(age is not None and 0<=age<=60 or recent_version)
    if not pub:reason.append("源记录发表日期在未来" if future_dates else "缺少可用的完整发表日期")
    elif age>60:reason.append("新收录或题录更新的较早论文")
    if p.get("retracted"):reason.append("来源标记撤稿或撤稿相关记录")
    status="core" if core and abstract else ("related" if matched else "query_candidate")
    topic_score=55 if core and hits(title,evidence.get("topic_terms",[]) or (AI if key=="AI_medical_imaging" else METHOD if key=="omics_methods_applications" else DATA+DESIGN+OUTCOME)) else (40 if core else 15)
    freshness=round(max(0,15-(age or 0)/2),2) if pub else (5 if recent_version else 0)
    type_score=-8 if commentary else (-4 if protocol else (4 if letter else (12 if method_cue else (8 if not review and not guideline else 0))))
    transparency=(5 if abstract else 0)+(5 if pub else 0)
    score=round(topic_score+freshness+type_score+transparency+application_bonus,2)
    eligible=bool(core and abstract and recent and not p.get("retracted") and (pub or recent_version))
    if p.get("kind")=="book":
        eligible=False;typ="书籍 / 报告候选";reason.append("书籍或报告记录，未纳入原始研究主榜")
    return {"track":key,"track_name":NAMES[key],"status":status,"eligible":eligible,"score":score,
       "labels":labels,"article_type":typ,"method_cue":method_cue,"evidence":evidence,"application_context":context,
       "score_components":{"topic_relevance":topic_score,"publication_recency":freshness,"article_type_cues":type_score,"metadata_availability":transparency,"application_priority":application_bonus},
       "published":pub.isoformat() if pub else None,"publication_age_days":age,"date_status":date_state,
       "recent_preprint_version":recent_version,"reasons":reason,"label":"规则生成的阅读优先级，未经人工质量评价；不是正式HOT"}

@functools.lru_cache(maxsize=256)
def parse_cached(q):return parse(q)

def diversified(rows,limit=30):
    ordered=sorted(rows,key=lambda x:(-x["ranking"]["score"],x["paper_id"]))
    if not ordered or ordered[0]["ranking"]["track"]!="omics_methods_applications":return ordered[:limit]
    chosen=[];used=set()
    for label in ["CUT&Tag / CUT&RUN","ATAC","GWAS","单细胞 / 单核","空间组学","RNA / 转录组"]:
        candidate=next((r for r in ordered if r["paper_id"] not in used and label in r["ranking"]["labels"] and (r["ranking"].get("application_context")!="plant_application" or r["ranking"].get("method_cue"))),None)
        if candidate:chosen.append(candidate);used.add(candidate["paper_id"])
        if len(chosen)>=limit:return chosen
    return (chosen+[r for r in ordered if r["paper_id"] not in used])[:limit]

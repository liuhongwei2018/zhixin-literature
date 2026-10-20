"""Public-source normalization, derived from the independently tested collector."""
import calendar,re,time,json,xml.etree.ElementTree as ET
from datetime import datetime,timezone

class SourceError(ValueError):
    pass
def doi(v):
    s=str(v or '').strip().lower()
    for p in ('https://doi.org/','http://doi.org/','doi:'):
        if s.startswith(p):s=s[len(p):].strip()
    return s if re.match(r'^10\.\d{4,9}/\S+$',s) else None

def text(el):return ' '.join(''.join(el.itertext()).split()) if el is not None else ''

def text_list(value):
    if value is None:return []
    if not isinstance(value,list):raise ValueError('metadata_text_list_not_list')
    if any(x is not None and not isinstance(x,str) for x in value):raise ValueError('metadata_text_list_invalid_item')
    return [x for x in value if isinstance(x,str) and x.strip()]

def epmc_row(p):
    pmid=p.get('pmid');d=doi(p.get('doi'));src=p.get('source');i=str(p.get('id') or '')
    return {'source':'epmc','id':str(src)+':'+i,'pmid':pmid,'pmcid':p.get('pmcid') or (i if src=='PMC' and i.startswith('PMC') else None),'doi':d,'title':p.get('title') or '',
      'abstract':re.sub('<[^>]+>',' ',p.get('abstractText') or ''),'keywords':text_list((p.get('keywordList') or {}).get('keyword',[])),
      'published':p.get('firstPublicationDate'),'indexed':p.get('firstIndexDate'),'updated':None,
      'kind':'preprint' if src=='PPR' else 'journal','version':'',
      'journal':((p.get('journalInfo') or {}).get('journal') or {}).get('title') or '',
      'publication_types':text_list((p.get('pubTypeList') or {}).get('pubType',[])),
      'url':'https://pubmed.ncbi.nlm.nih.gov/'+pmid+'/' if pmid else ('https://doi.org/'+d if d else 'https://europepmc.org/article/'+str(src)+'/'+i),
      'citations':p.get('citedByCount'),'retracted':p.get('isRetracted')=='Y'}

def arxiv_page(xml):
    ns={'a':'http://www.w3.org/2005/Atom','o':'http://a9.com/-/spec/opensearch/1.1/','x':'http://arxiv.org/schemas/atom'}
    r=ET.fromstring(xml);total=int(r.findtext('o:totalResults',namespaces=ns));out=[]
    for e in r.findall('a:entry',ns):
        url=e.findtext('a:id',namespaces=ns) or '';ident=url.split('/abs/')[-1]
        if not re.match(r'^(?:\d{4}\.\d{4,5}|[a-z-]+/\d{7})v\d+$',ident):raise SourceError('arxiv_invalid_id')
        base=re.sub(r'v\d+$','',ident)
        out.append({'source':'arxiv','id':base,'arxiv':base,'version':ident[len(base):],
           'doi':doi(e.findtext('x:doi',namespaces=ns)),'title':text(e.find('a:title',ns)),
           'abstract':text(e.find('a:summary',ns)),'published':e.findtext('a:published',namespaces=ns),
           'updated':e.findtext('a:updated',namespaces=ns),'kind':'preprint','url':'https://arxiv.org/abs/'+ident,'keywords':[]})
    return total,out

def dates(node):
    y=node.findtext("Year");m=node.findtext("Month");d=node.findtext("Day")
    if not y:return node.findtext("MedlineDate") or ""
    if m and not m.isdigit():
        names={v.lower():str(i) for i,v in enumerate(calendar.month_abbr) if v};m=names.get(m[:3].lower())
    return "-".join([y]+([m.zfill(2)] if m else [])+([d.zfill(2)] if d and m else []))

def pubmed_rows(xml,ids):
    root=ET.fromstring(xml)
    if root.tag!="PubmedArticleSet" or root.find("ERROR") is not None:raise SourceError("invalid_pubmed_xml")
    rows=[];observed=[]
    for record in root:
        if record.tag=="PubmedArticle":
            med=record.find("MedlineCitation");article=med.find("Article");pmid=med.findtext("PMID")
            pds=[{"value":dates(x),"type":"electronic" if x.get("DateType")=="Electronic" else "article_date"} for x in article.findall("ArticleDate")]
            pds += [{"value":dates(x),"type":"journal_issue"} for x in article.findall("Journal/JournalIssue/PubDate")]
            pubtypes=[text(x) for x in article.findall("PublicationTypeList/PublicationType")]
            doi_ids=[x.text for x in record.findall("PubmedData/ArticleIdList/ArticleId") if x.get("IdType")=="doi"]
            pmc_ids=[x.text for x in record.findall("PubmedData/ArticleIdList/ArticleId") if x.get("IdType")=="pmc"]
            keywords=[text(x) for x in med.findall("KeywordList/Keyword")]
            a=text(article.find("ArticleTitle"));abstract=" ".join(text(x) for x in article.findall("Abstract/AbstractText"))
            history={x.get("PubStatus"):dates(x) for x in record.findall("PubmedData/History/PubMedPubDate")}
            retracted=any(x in pubtypes for x in ("Retracted Publication","Retraction of Publication")) or any(x.get("RefType") in ("RetractionIn","RetractionOf") for x in med.findall("CommentsCorrectionsList/CommentsCorrections"))
            journal=text(article.find("Journal/Title"))
            kind="preprint" if "Preprint" in pubtypes else "journal"
        elif record.tag=="PubmedBookArticle":
            data=record.find("BookDocument");pmid=data.findtext("PMID");a=text(data.find("ArticleTitle")) or text(data.find("Book/BookTitle"))
            abstract=" ".join(text(x) for x in data.findall("Abstract/AbstractText"));doi_ids=[];pmc_ids=[];keywords=[]
            pds=[{"value":dates(x),"type":"book"} for x in data.findall("Book/PubDate")];pubtypes=["Book"];history={};retracted=False;journal="";kind="book"
        else:raise SourceError("unsupported_pubmed_record_type")
        if not pmid:raise SourceError("pubmed_missing_id")
        observed.append(pmid);published=next((x["value"] for x in pds if re.fullmatch(r"\d{4}-\d{2}-\d{2}",x["value"])),pds[0]["value"] if pds else None)
        rows.append({"source":"pubmed","id":pmid,"pmid":pmid,"doi":doi(doi_ids[0]) if doi_ids else None,"pmcid":pmc_ids[0] if pmc_ids else None,
            "title":a,"abstract":abstract,"keywords":keywords,"publication_dates":pds,"publication_types":pubtypes,"publication_history":history,"journal":journal,
            "published":published,"updated":None,"kind":kind,"version":"","url":"https://pubmed.ncbi.nlm.nih.gov/"+pmid+"/","retracted":retracted})
    if len(observed)!=len(set(observed)) or set(observed)!=set(ids):raise SourceError("pubmed_fetch_id_mismatch")
    return rows

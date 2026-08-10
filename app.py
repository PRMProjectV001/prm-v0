import json, re, math
from datetime import datetime, timezone
import pandas as pd
import requests
from pypdf import PdfReader
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

import streamlit as st
from shapely.geometry import Point, shape, mapping
from shapely.ops import unary_union

st.set_page_config(page_title="PRM - Property Risk Management", page_icon="🏠", layout="wide")

GEOCODE_SEARCH="https://data.geopf.fr/geocodage/search"
GEOCODE_REVERSE="https://data.geopf.fr/geocodage/reverse"
CADASTRE_URL="https://apicarto.ign.fr/api/cadastre/parcelle"
GPU_BASE="https://apicarto.ign.fr/api/gpu"
GEORISQUES_REPORT="https://georisques.gouv.fr/api/v1/resultats_rapport_risque"
GEORISQUES_PDF="https://georisques.gouv.fr/api/v1/rapport_pdf"
BDNB_BASE="https://api.bdnb.io/v1/bdnb"
DATAFAIR_BASE="https://opendata.koumoul.com/data-fair/api/v1/datasets"
TIMEOUT=22

SITADEL_DATASETS = {
    "Logements / DP-PC": "sitadel-logements",
    "Locaux / DP-PC": "sitadel-locaux",
    "Permis d’aménager": "sitadel-pa",
    "Permis de démolir": "sitadel-pd",
}

def api_get(url,params=None):
    try:
        r=requests.get(url,params=params,timeout=TIMEOUT,headers={"User-Agent":"PRM-V0.6"})
        r.raise_for_status()
        return r.json(),None
    except Exception as e:
        return None,str(e)

def now_fr():
    return datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M")

# ---------------- Adresse / parcelles ----------------
def geocode(address):
    data,err=api_get(GEOCODE_SEARCH,{"q":address,"index":"address","limit":5,"autocomplete":"false"})
    if err or not data or not data.get("features"):
        return None,err or "Adresse introuvable."
    f=data["features"][0]; lon,lat=f["geometry"]["coordinates"]; p=f.get("properties",{})
    return {"label":p.get("label") or address,"lon":lon,"lat":lat,"citycode":p.get("citycode"),"postcode":p.get("postcode"),"city":p.get("city"),"score":p.get("score")},None

def reverse_parcel(lon,lat):
    data,err=api_get(GEOCODE_REVERSE,{"lon":lon,"lat":lat,"index":"parcel","limit":10})
    return ((data or {}).get("features",[]) if data else []),err

def get_candidates(lon,lat):
    d=0.00022
    poly={"type":"Polygon","coordinates":[[[lon-d,lat-d],[lon+d,lat-d],[lon+d,lat+d],[lon-d,lat+d],[lon-d,lat-d]]]}
    data,err=api_get(CADASTRE_URL,{"geom":json.dumps(poly,separators=(",",":")),"_limit":60})
    feats=(data or {}).get("features",[]) if data else []
    pt=Point(lon,lat); scored=[]
    for f in feats:
        try: scored.append((shape(f["geometry"]).distance(pt)*111000,f))
        except: pass
    scored.sort(key=lambda x:x[0])
    return scored[:20],err

def parcel_id(f):
    p=f.get("properties",{}); sec=str(p.get("section") or "").strip(); num=str(p.get("numero") or p.get("numero_parcelle") or "").strip()
    if num.isdigit(): num=num.zfill(4)
    return f"{sec} {num}".strip()

def reverse_ids(features):
    out=set()
    for f in features:
        p=f.get("properties",{}); pid=str(p.get("id") or "")
        m=re.search(r"([A-Z]{1,2})(\d{4})$",pid)
        if m: out.add(f"{m.group(1)} {m.group(2)}")
    return out

def recursive_find(obj,key_contains=None):
    vals=[]
    def walk(x):
        if isinstance(x,dict):
            for k,v in x.items():
                if key_contains and key_contains.lower() in str(k).lower(): vals.append(v)
                walk(v)
        elif isinstance(x,list):
            for v in x: walk(v)
    walk(obj); return vals

def bdnb_address_key(address):
    data,err=api_get(f"{BDNB_BASE}/geocodage",{"q":address})
    if err or not data:return None,err
    vals=recursive_find(data,"cle_interop_adr")
    for v in vals:
        if isinstance(v,str) and v.strip():return v.strip(),None
    return None,"Clé BAN non trouvée"

def parcel_ids_from_bdnb(obj):
    out=set()
    def inspect(v):
        s=str(v).upper()
        for m in re.finditer(r"([A-Z]{1,2})\s*0*(\d{1,4})\b",s):out.add(f"{m.group(1)} {m.group(2).zfill(4)}")
        for m in re.finditer(r"[0-9A-Z]{8,12}([A-Z]{1,2})(\d{4})\b",s):out.add(f"{m.group(1)} {m.group(2)}")
    def walk(x,k=""):
        if isinstance(x,dict):
            for kk,v in x.items():
                if "parcelle" in str(kk).lower() or "cadastr" in str(kk).lower():inspect(v)
                walk(v,str(kk))
        elif isinstance(x,list):
            for v in x:walk(v,k)
        elif "parcelle" in k.lower() or "cadastr" in k.lower():inspect(x)
    walk(obj);return out

def bdnb_resolve(address):
    key,kerr=bdnb_address_key(address)
    if not key:return set(),{"geocodage":kerr}
    endpoint=f"{BDNB_BASE}/donnees/batiment_groupe_complet/adresse"
    for params in [{"cle_interop_adr":f"eq.{key}","limit":20},{"cle_interop_adr":key,"limit":20}]:
        data,err=api_get(endpoint,params)
        if not err and data:
            ids=parcel_ids_from_bdnb(data)
            if ids:return ids,{"cle_interop_adr":key}
    return set(),{"cle_interop_adr":key,"status":"pas de parcelle extraite"}

def strict_reverse_selection(candidates,rids):
    matched=[(d,f) for d,f in candidates if parcel_id(f) in rids]
    if not matched:return []
    matched.sort(key=lambda x:x[0]); min_d=matched[0][0]; threshold=min_d+1.35
    return [f for d,f in matched if d<=threshold][:3]

def auto_resolve(address,lon,lat):
    candidates,cerr=get_candidates(lon,lat)
    bdnb_ids,bdnb_meta=bdnb_resolve(address)
    bdnb_matches=[f for d,f in candidates if parcel_id(f) in bdnb_ids]
    reasons={}
    if bdnb_matches:
        for f in bdnb_matches:reasons[parcel_id(f)]="Liée au bâtiment/adresse dans la BDNB"
        return bdnb_matches[:3],reasons,"élevée","BDNB",{"cadastre":cerr,"bdnb":bdnb_meta},candidates
    rev,rerr=reverse_parcel(lon,lat); strict=strict_reverse_selection(candidates,reverse_ids(rev))
    if strict:
        for f in strict:reasons[parcel_id(f)]="Géocodage inverse IGN + filtre strict"
        return strict,reasons,"moyenne","IGN reverse strict",{"cadastre":cerr,"reverse":rerr,"bdnb":bdnb_meta},candidates
    if candidates:
        d,f=candidates[0];reasons[parcel_id(f)]=f"Parcelle la plus proche (~{d:.1f} m), à confirmer"
        return [f],reasons,"faible","fallback proximité",{"cadastre":cerr,"bdnb":bdnb_meta},candidates
    return [],reasons,"faible","échec",{"cadastre":cerr,"bdnb":bdnb_meta},candidates

def union_geom(features):
    return unary_union([shape(f["geometry"]) for f in features]).simplify(0.000001,preserve_topology=True)

def geojson_str(g):return json.dumps(mapping(g),separators=(",",":"))

# ---------------- Urbanisme ----------------
def gpu_layer(layer,g):
    data,err=api_get(f"{GPU_BASE}/{layer}",{"geom":geojson_str(g),"_limit":200})
    return ((data or {}).get("features",[]) if data else []),err

def get_urbanism(g):
    layers={"zones":"zone-urba","prescriptions_surface":"prescription-surf","prescriptions_line":"prescription-lin","prescriptions_point":"prescription-pct","infos_surface":"info-surf"}
    out={};errs={}
    for k,l in layers.items():
        out[k],e=gpu_layer(l,g)
        if e:errs[k]=e
    return out,errs

def zone_labels(features):
    labels=[]
    for f in features:
        p=f.get("properties",{})
        for k in ["libelle","libelle_zone","typezone","type_zone","nom","name"]:
            v=p.get(k)
            if isinstance(v,str) and v.strip():labels.append(v.strip())
    labels=list(dict.fromkeys(labels));specific=[x for x in labels if len(x)>1]
    return specific or labels

# ---------------- Géorisques ----------------
@st.cache_data(ttl=1800, show_spinner=False)
def get_reports_stable(lon,lat):
    """
    Effectue plusieurs lectures du rapport Géorisques.
    Le but n'est pas de fabriquer une moyenne, mais de détecter
    les réponses partielles/incohérentes et de refuser de conclure.
    """
    reports=[]
    errors=[]
    for _ in range(3):
        data,err=api_get(GEORISQUES_REPORT,{"latlon":f"{lon},{lat}"})
        if data:
            reports.append(data)
        if err:
            errors.append(err)
    return reports,errors

def norm(x):return " ".join(str(x or "").lower().split())

def subtrees(obj,aliases):
    aliases=[a.lower() for a in aliases];hits=[]
    def walk(x,path=""):
        if isinstance(x,dict):
            nameish=" ".join(norm(x.get(k)) for k in x if str(k).lower() in ["libelle","nom","name","type","risque","libelle_risque","code_risque","titre","title"])
            context=f"{path} {' '.join(str(k).lower() for k in x)} {nameish}"
            if any(a in context for a in aliases):hits.append(x)
            for k,v in x.items():walk(v,f"{path}/{str(k).lower()}")
        elif isinstance(x,list):
            for i,v in enumerate(x):walk(v,f"{path}/{i}")
    walk(obj);uniq=[];seen=set()
    for h in hits:
        s=json.dumps(h,sort_keys=True,ensure_ascii=False,default=str)
        if s not in seen:seen.add(s);uniq.append(h)
    return uniq

def subtree_text(ss):
    vals=[]
    def walk(x):
        if isinstance(x,dict):
            for k,v in x.items():vals.append(str(k));walk(v)
        elif isinstance(x,list):
            for v in x:walk(v)
        elif isinstance(x,(str,int,float,bool)):vals.append(str(x))
    for s in ss:walk(s)
    return norm(" | ".join(vals))


def report_text(report):
    """Flatten the complete Géorisques payload, keys + scalar values."""
    vals=[]
    def walk(x):
        if isinstance(x,dict):
            for k,v in x.items():
                vals.append(str(k))
                walk(v)
        elif isinstance(x,list):
            for v in x:
                walk(v)
        elif isinstance(x,(str,int,float,bool)):
            vals.append(str(x))
    walk(report)
    return norm(" | ".join(vals))

def first_int(text, patterns, low, high):
    for pat in patterns:
        m=re.search(pat,text,re.I)
        if m:
            try:
                n=int(m.group(1))
                if low <= n <= high:
                    return n
            except Exception:
                pass
    return None

def nearby_count(text, label_patterns, max_radius=500):
    """
    Extract explicit wording such as:
      '50 ancien(s) site(s) ... à moins de 500 m'
      '2 installation(s) ... à moins de 500 m'
    Only returns a count when the report explicitly ties it to a distance.
    """
    for lab in label_patterns:
        patterns=[
            rf"(\d+)\s+{lab}.{{0,120}}(?:a|à)\s+moins\s+de\s+(\d+)\s*m",
            rf"(\d+)\s+{lab}.{{0,120}}dans\s+un\s+rayon\s+de\s+(\d+)\s*m",
        ]
        for pat in patterns:
            m=re.search(pat,text,re.I)
            if m:
                try:
                    count=int(m.group(1)); radius=int(m.group(2))
                    if radius <= max_radius:
                        return count,radius
                except Exception:
                    pass
    return None,None

def scale(text,den):
    m=re.search(rf"\b([1-{den}])\s*/\s*{den}\b",text)
    if m:return int(m.group(1))
    for pat in [r"\bniveau\s*[:=]?\s*([1-5])\b",r"\bzone\s*[:=]?\s*([1-5])\b"]:
        m=re.search(pat,text)
        if m:
            n=int(m.group(1))
            if 1<=n<=den:return n
    return None

def item(status,label,source="Géorisques",scope="adresse",confidence="moyenne",official_level=None,details=None):
    return {"status":status,"label":label,"source":source,"scope":scope,"confidence":confidence,"official_level":official_level,"details":details or [],"checked_at":now_fr()}

def classify_radon(report):
    # Prefer the complete official report wording. Radon is a commune-level
    # regulatory potential (1/3 to 3/3), not a parcel measurement.
    full=report_text(report)
    local=subtree_text(subtrees(report,["radon"]))
    t=full+" | "+local

    n=first_int(t,[
        r"potentiel\s+radon\s+(?:est|de|:)?\s*(?:de\s*)?([1-3])\s*/\s*3",
        r"radon.{0,120}?([1-3])\s*/\s*3",
        r"niveau\s+radon\s*[:=]?\s*([1-3])",
    ],1,3)
    if n is None:
        n=scale(local,3) if local else None

    if n==1 or "potentiel radon faible" in t:
        return item(
            "🟢","Potentiel radon faible",
            scope="commune",confidence="élevée",official_level="1/3",
            details=["Échelle réglementaire Géorisques : 1/3."]
        )
    if n==2 or "potentiel radon moyen" in t or "potentiel radon intermédiaire" in t:
        return item(
            "🟠","Potentiel radon intermédiaire",
            scope="commune",confidence="élevée",official_level="2/3",
            details=["Échelle réglementaire Géorisques : 2/3."]
        )
    if n==3 or "potentiel radon élevé" in t or "potentiel radon eleve" in t:
        return item(
            "🔴","Potentiel radon élevé",
            scope="commune",confidence="élevée",official_level="3/3",
            details=[
                "Échelle réglementaire Géorisques : 3/3.",
                "Le potentiel radon décrit le territoire ; la concentration réelle dans un bâtiment nécessite une mesure."
            ]
        )
    return item(
        "⚪","Niveau radon officiel non extrait",
        scope="commune",confidence="faible",
        details=["PRM n’invente pas de niveau lorsque l’échelle réglementaire n’est pas lisible dans la réponse Géorisques."]
    )

def classify_seisme(report):
    # Official regulatory seismic zoning: 1/5 to 5/5.
    full=report_text(report)
    local=subtree_text(subtrees(report,["séisme","seisme","sismique"]))
    t=full+" | "+local

    n=first_int(t,[
        r"risque\s+sismique\s+(?:est|de|:)?\s*(?:de\s*)?([1-5])\s*/\s*5",
        r"sismicite.{0,120}?([1-5])\s*/\s*5",
        r"sismicité.{0,120}?([1-5])\s*/\s*5",
        r"seisme.{0,120}?([1-5])\s*/\s*5",
        r"séisme.{0,120}?([1-5])\s*/\s*5",
    ],1,5)
    if n is None:
        n=scale(local,5) if local else None

    labels={
        1:("🟢","Sismicité très faible"),
        2:("🟢","Sismicité faible"),
        3:("🟠","Sismicité modérée"),
        4:("🟠","Sismicité moyenne"),
        5:("🔴","Sismicité forte"),
    }
    if n in labels:
        status,label=labels[n]
        details=[f"Zonage réglementaire Géorisques : {n}/5."]
        if n >= 2:
            details.append("Des règles parasismiques peuvent s’appliquer selon le type de bâtiment et les travaux.")
        return item(
            status,label,scope="adresse / zonage communal",
            confidence="élevée",official_level=f"{n}/5",details=details
        )
    return item(
        "⚪","Zonage sismique officiel non extrait",
        scope="adresse / zonage communal",confidence="faible",
        details=["PRM suspend la conclusion plutôt que d’interpréter une simple mention du risque."]
    )

def classify_argile(report):
    t=subtree_text(subtrees(report,["argile","retrait gonflement","gonflement"])); n=scale(t,3) if t else None
    if n==1 or "faible" in t:return item("🟢","Exposition argiles faible",official_level="Faible")
    if n==2 or "moyen" in t:return item("🟠","Exposition argiles moyenne",official_level="Moyenne")
    if n==3 or "forte" in t:return item("🔴","Exposition argiles forte",official_level="Forte")
    return item("⚪","Argiles mentionnées, niveau non interprétable",confidence="faible")

def classify_inondation(report):
    t=subtree_text(subtrees(report,["inondation","crue","submersion","remontée de nappe","remontee de nappe"]))
    if any(x in t for x in ["non concerné","non concerne","hors zone","faible"]):return item("🟢","Faible exposition détectée",official_level="Faible / non concerné")
    if any(x in t for x in ["zone rouge","aléa fort","alea fort","affecte votre bien"]):return item("🔴","Exposition significative détectée",official_level="Forte")
    if any(x in t for x in ["zone bleue","aléa moyen","alea moyen"]):return item("🟠","Élément d’inondation à vérifier",official_level="Intermédiaire")
    return item("⚪","Données d’inondation présentes, niveau à préciser",confidence="faible")

def classify_mouvements(report):
    t=subtree_text(subtrees(report,["mouvement de terrain","cavité","cavite","effondrement","glissement"]))
    return item("⚪","Aucun événement précis récupéré" if not t else "Historique présent, exposition parcellaire non prouvée",confidence="faible")

def classify_tech(report):
    """
    Avoid false positives caused by generic report headings.
    We only elevate a technological signal when the report contains an explicit
    statement, count, or proximity wording.
    """
    t=report_text(report)

    details=[]
    statuses=[]

    # ICPE: explicit counts or wording.
    icpe_commune=None
    patterns=[
        r"(\d+)\s+installation\(s\)\s+classée\(s\).{0,160}?sur\s+la\s+commune",
        r"(\d+)\s+installation(?:s)?\s+class[ée]e(?:s)?.{0,160}?sur\s+la\s+commune",
    ]
    for pat in patterns:
        m=re.search(pat,t,re.I)
        if m:
            icpe_commune=int(m.group(1)); break

    icpe_near,icpe_radius=nearby_count(
        t,[r"installation(?:s)?\s+class[ée]e(?:s)?",r"icpe(?:s)?"]
    )

    if icpe_near is not None:
        details.append(f"ICPE : {icpe_near} installation(s) explicitement signalée(s) à moins de {icpe_radius} m.")
        statuses.append("🟠" if icpe_near > 0 else "🟢")
    elif icpe_commune is not None:
        details.append(f"ICPE : {icpe_commune} installation(s) signalée(s) à l’échelle de la commune ; distance au bien non fournie.")
        statuses.append("⚪" if icpe_commune > 0 else "🟢")
    elif re.search(r"installation(?:s)?\s+industrielle(?:s)?\s+class[ée]e(?:s)?.{0,180}?(?:aucune|0)",t,re.I):
        details.append("ICPE : aucun établissement explicitement signalé dans le passage exploitable.")
        statuses.append("🟢")

    # Dangerous-material pipelines: report may identify the theme without giving distance.
    pipeline_explicit=bool(re.search(
        r"canalisation(?:s)?\s+de\s+transport\s+de\s+mati[eè]res?\s+dangereuses?",
        t,re.I
    ))
    pipe_near,pipe_radius=nearby_count(
        t,[r"canalisation(?:s)?(?:\s+de\s+transport)?"]
    )
    if pipe_near is not None:
        details.append(f"Canalisation : {pipe_near} objet(s) explicitement signalé(s) à moins de {pipe_radius} m.")
        statuses.append("🟠" if pipe_near > 0 else "🟢")
    elif pipeline_explicit:
        details.append("Canalisation de transport : rubrique présente, mais aucune distance exploitable n’est fournie dans la réponse.")
        statuses.append("⚪")

    # Nuclear: require explicit exposure wording; do not treat a generic word occurrence as local exposure.
    nuclear_local=bool(re.search(
        r"(?:risque|installation|centrale).{0,80}nucl[ée]aire.{0,180}?(?:adresse|commune|moins de|rayon|concern)",
        t,re.I
    ))
    if nuclear_local:
        details.append("Nucléaire : signal local explicite trouvé dans le rapport ; distance à confirmer si elle n’est pas chiffrée.")
        statuses.append("⚪")

    if not details:
        return item(
            "🟢","Aucun signal technologique local exploitable extrait",
            confidence="moyenne",
            details=["Les simples titres ou rubriques génériques du rapport ne sont plus considérés comme une exposition."]
        )

    if "🟠" in statuses:
        return item(
            "🟠","Signal technologique de proximité à vérifier",
            confidence="moyenne",details=details
        )
    if all(s=="🟢" for s in statuses):
        return item(
            "🟢","Aucun signal technologique significatif extrait",
            confidence="moyenne",details=details
        )
    return item(
        "⚪","Contexte technologique identifié, proximité non établie",
        confidence="moyenne",details=details
    )



@st.cache_data(ttl=1800, show_spinner=False)
def get_georisques_pdf_text(lon,lat):
    """
    Read the official Géorisques PDF report for the exact geocoded point.
    This report often exposes regulatory wording (radon x/3, seismicity x/5)
    more explicitly than the JSON response.
    """
    try:
        r=requests.get(
            GEORISQUES_PDF,
            params={"latlon":f"{lon},{lat}"},
            timeout=TIMEOUT
        )
        r.raise_for_status()
        reader=PdfReader(BytesIO(r.content))
        chunks=[]
        for page in reader.pages:
            try:
                txt=page.extract_text() or ""
                if txt:
                    chunks.append(txt)
            except Exception:
                continue
        return norm(" | ".join(chunks)), None
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"

def classify_radon_text(pdf_text):
    t=norm(pdf_text or "")
    n=first_int(t,[
        r"potentiel\s+radon\s+(?:est|de|:)?\s*(?:de\s*)?([1-3])\s*/\s*3",
        r"radon.{0,140}?([1-3])\s*/\s*3",
    ],1,3)
    labels={
        1:("🟢","Potentiel radon faible"),
        2:("🟠","Potentiel radon intermédiaire"),
        3:("🔴","Potentiel radon élevé"),
    }
    if n in labels:
        status,label=labels[n]
        details=[
            f"Échelle réglementaire Géorisques : {n}/3.",
            "Lecture extraite du rapport PDF officiel généré pour le point géocodé."
        ]
        if n==3:
            details.append("Le potentiel territorial ne remplace pas une mesure de concentration dans le bâtiment.")
        return item(
            status,label,scope="commune",confidence="élevée",
            official_level=f"{n}/3",details=details
        )
    return None

def classify_seisme_text(pdf_text):
    t=norm(pdf_text or "")
    n=first_int(t,[
        r"risque\s+sismique\s+(?:est|de|:)?\s*(?:de\s*)?([1-5])\s*/\s*5",
        r"sismicite.{0,140}?([1-5])\s*/\s*5",
        r"sismicité.{0,140}?([1-5])\s*/\s*5",
    ],1,5)
    labels={
        1:("🟢","Sismicité très faible"),
        2:("🟢","Sismicité faible"),
        3:("🟠","Sismicité modérée"),
        4:("🟠","Sismicité moyenne"),
        5:("🔴","Sismicité forte"),
    }
    if n in labels:
        status,label=labels[n]
        details=[
            f"Zonage réglementaire Géorisques : {n}/5.",
            "Lecture extraite du rapport PDF officiel généré pour le point géocodé."
        ]
        if n>=2:
            details.append("Des règles parasismiques peuvent s’appliquer selon le bâtiment et les travaux.")
        return item(
            status,label,scope="adresse / zonage communal",confidence="élevée",
            official_level=f"{n}/5",details=details
        )
    return None


def extract_tech_pdf_signals(pdf_text):
    """
    Extract only explicit local/proximity technological signals from the
    official Géorisques PDF. Generic headings are ignored.
    """
    t=norm(pdf_text or "")
    signals=[]

    # ICPE explicit proximity/count wording.
    icpe_patterns=[
        r"(\d+)\s+installation(?:s)?\s+class[ée]e(?:s)?.{0,180}?(?:a|à)\s+moins\s+de\s+(\d+)\s*m",
        r"(\d+)\s+icpe(?:s)?.{0,180}?(?:a|à)\s+moins\s+de\s+(\d+)\s*m",
        r"(\d+)\s+installation(?:s)?\s+class[ée]e(?:s)?.{0,180}?dans\s+un\s+rayon\s+de\s+(\d+)\s*m",
    ]
    for pat in icpe_patterns:
        m=re.search(pat,t,re.I)
        if m:
            signals.append({
                "type":"ICPE",
                "count":int(m.group(1)),
                "radius_m":int(m.group(2)),
                "explicit":True
            })
            break

    # Some Géorisques reports phrase the absence explicitly.
    if not any(s["type"]=="ICPE" for s in signals):
        if re.search(
            r"(?:aucune|0)\s+installation(?:s)?\s+class[ée]e(?:s)?.{0,160}?(?:moins de|rayon)",
            t,re.I
        ):
            signals.append({"type":"ICPE","count":0,"radius_m":500,"explicit":True})

    # Pipelines: only accept explicit distance/radius wording.
    pipe_patterns=[
        r"canalisation(?:s)?\s+de\s+transport.{0,220}?(?:a|à)\s+moins\s+de\s+(\d+)\s*m",
        r"canalisation(?:s)?\s+de\s+transport.{0,220}?dans\s+un\s+rayon\s+de\s+(\d+)\s*m",
        r"distance.{0,80}?canalisation.{0,80}?(\d+)\s*m",
    ]
    for pat in pipe_patterns:
        m=re.search(pat,t,re.I)
        if m:
            signals.append({
                "type":"Canalisation",
                "distance_or_radius_m":int(m.group(1)),
                "explicit":True
            })
            break

    # Nuclear: require explicit address/commune-level concern or distance.
    nuc_distance=re.search(
        r"(?:installation|centrale|risque)\s+nucl[ée]aire.{0,220}?(?:a|à)\s+moins\s+de\s+(\d+)\s*(?:m|km)",
        t,re.I
    )
    if nuc_distance:
        raw=int(nuc_distance.group(1))
        snippet=nuc_distance.group(0)
        dist_m=raw*1000 if "km" in snippet.lower() else raw
        signals.append({
            "type":"Nucléaire",
            "distance_or_radius_m":dist_m,
            "explicit":True
        })
    elif re.search(
        r"(?:commune|adresse).{0,120}?(?:concern[ée]e?|soumise).{0,120}?nucl[ée]aire",
        t,re.I
    ):
        signals.append({"type":"Nucléaire","local":True,"explicit":True})

    return signals

def classify_tech_pdf(pdf_text):
    signals=extract_tech_pdf_signals(pdf_text)
    if not signals:
        return None

    details=[]
    severity="🟢"

    for s in signals:
        typ=s["type"]

        if typ=="ICPE":
            count=s.get("count")
            radius=s.get("radius_m")
            if count==0:
                details.append(f"ICPE : aucune installation explicitement signalée dans le rayon de {radius} m.")
            elif count is not None and radius:
                details.append(f"ICPE : {count} installation(s) explicitement signalée(s) dans un rayon de {radius} m.")
                if radius<=500 and count>0:
                    severity="🟠"

        elif typ=="Canalisation":
            d=s.get("distance_or_radius_m")
            details.append(f"Canalisation de transport : proximité explicitement mentionnée à environ {d} m ou dans ce rayon.")
            if d is not None and d<=500:
                severity="🟠"

        elif typ=="Nucléaire":
            d=s.get("distance_or_radius_m")
            if d is not None:
                details.append(f"Nucléaire : signal explicite dans le rapport à environ {d/1000:.1f} km.")
                if d<=5000:
                    severity="🟠"
            else:
                details.append("Nucléaire : le rapport signale explicitement un contexte local, sans distance exploitable.")
                if severity=="🟢":
                    severity="⚪"

    if severity=="🟠":
        return item(
            "🟠","Signal technologique de proximité explicitement documenté",
            scope="adresse / proximité",confidence="élevée",
            details=details+["Lecture extraite du rapport PDF officiel Géorisques."]
        )

    if severity=="⚪":
        return item(
            "⚪","Contexte technologique local documenté, distance à préciser",
            scope="adresse / commune",confidence="moyenne",
            details=details+["Lecture extraite du rapport PDF officiel Géorisques."]
        )

    return item(
        "🟢","Aucun signal technologique proche extrait du rapport",
        scope="adresse / proximité",confidence="moyenne",
        details=details+["Lecture extraite du rapport PDF officiel Géorisques."]
    )


# ---------------- Stabilisation des risques ----------------
STATUS_RANK={"🟢":0,"⚪":1,"🟠":2,"🔴":3}

def risk_signature(x):
    return (x.get("status"), x.get("label"), x.get("official_level"))

def stable_risk(classifier,reports,risk_name):
    """
    Règles PRM :
    - 0 rapport exploitable -> gris
    - 3 lectures identiques -> résultat accepté, confiance renforcée
    - 2 lectures identiques sur 3 -> résultat accepté, confiance moyenne
    - désaccord sans majorité -> gris, jamais de faux vert/orange/rouge
    """
    if not reports:
        return item(
            "⚪",
            f"{risk_name} : source momentanément indisponible",
            confidence="aucune",
            details=["Aucune réponse Géorisques exploitable pendant cette analyse."]
        )

    results=[]
    for r in reports:
        try:
            results.append(classifier(r))
        except Exception as e:
            results.append(item("⚪",f"{risk_name} : lecture impossible",confidence="faible"))

    counts={}
    for res in results:
        sig=risk_signature(res)
        counts[sig]=counts.get(sig,0)+1

    best_sig,best_count=max(counts.items(),key=lambda kv:kv[1])

    if best_count>=2:
        chosen=next(x for x in results if risk_signature(x)==best_sig)
        chosen=dict(chosen)
        chosen["confidence"]="élevée" if best_count==3 else "moyenne"
        chosen["details"]=list(chosen.get("details") or [])
        chosen["details"].append(f"Stabilité Géorisques : {best_count}/{len(results)} lectures concordantes")
        return chosen

    # No majority: do not let one transient response create a traffic light.
    labels=[]
    for x in results:
        s=f"{x.get('status')} {x.get('label')}"
        if s not in labels:
            labels.append(s)
    return item(
        "⚪",
        f"{risk_name} : données instables, conclusion suspendue",
        confidence="faible",
        details=[
            "Les lectures Géorisques reçues pendant cette analyse ne concordent pas.",
            "PRM refuse de choisir arbitrairement un niveau.",
            "Lectures observées : "+" | ".join(labels[:3])
        ]
    )

def stable_current_risks(reports):
    return {
        "Inondation / nappe":stable_risk(classify_inondation,reports,"Inondation / nappe"),
        "Argiles / sols":stable_risk(classify_argile,reports,"Argiles / sols"),
        "Mouvements / cavités":stable_risk(classify_mouvements,reports,"Mouvements / cavités"),
        "Séisme":stable_risk(classify_seisme,reports,"Séisme"),
        "Radon":stable_risk(classify_radon,reports,"Radon"),
        "Risques technologiques":stable_risk(classify_tech,reports,"Risques technologiques"),
    }



# ---------------- Restitution premium / PDF ----------------
def plain_status(status):
    return {
        "🟢":"Faible / favorable",
        "🟠":"Vigilance",
        "🔴":"Vigilance forte",
        "⚪":"A compléter",
    }.get(status,"A compléter")

def premium_summary(cats, current_red, current_orange, current_gray,
                    resolver_source, resolver_conf, priority_projects,
                    climate_profile, verify_lines):
    strengths=[]
    watch=[]
    actions=[]

    # Points rassurants = uniquement éléments interprétés verts.
    for name,x in cats.items():
        if name in ["Urbanisme","Climat 2050"]:
            continue
        if x.get("status")=="🟢":
            level=x.get("official_level")
            strengths.append(
                f"{name} : {x.get('label')}" + (f" ({level})" if level else "")
            )

    # A SURVEILLER = constats, incertitudes, contexte.
    for name in current_red+current_orange:
        x=cats[name]
        watch.append(f"{name} : {x.get('label')}")

    for name in current_gray:
        x=cats[name]
        watch.append(f"{name} : {x.get('label')}")

    if resolver_conf!="élevée":
        watch.append(
            f"Identification parcellaire : confiance {resolver_conf} ({resolver_source})."
        )

    if priority_projects:
        nearest=min(priority_projects,key=lambda p:p["distance_m"])
        watch.append(
            f"Projets voisins : {len(priority_projects)} autorisation(s) prioritaire(s), "
            f"la plus proche à {nearest['distance_m']} m."
        )

    if climate_profile.get("available"):
        watch.append(
            "Horizon 2050 : adaptation à anticiper pour chaleur, eau, pluies intenses et végétation."
        )

    # PRIORITES = actions concrètes uniquement.
    # 1) Mouvements/cavités
    if "Mouvements / cavités" in current_gray:
        actions.append(
            "Vérifier l’état des risques et mouvements de terrain sur les documents Géorisques / mairie "
            "si ce point est déterminant pour l’achat."
        )

    # 2) Risques technologiques
    if "Risques technologiques" in current_gray:
        actions.append(
            "Confirmer auprès des sources officielles la présence et la distance des canalisations "
            "ou installations technologiques avant engagement."
        )

    # 3) Végétation
    veg=cats.get("Végétation / vulnérabilité feu",{})
    if veg.get("status")=="⚪":
        actions.append(
            "Ajouter des photos du jardin, des façades et de la toiture pour compléter l’inspection végétation / feu."
        )
    elif veg.get("status") in ["🟠","🔴"]:
        actions.append(
            "Traiter les facteurs de vulnérabilité végétation / feu identifiés avant ou après acquisition."
        )

    # 4) Parcelles
    if resolver_conf!="élevée":
        actions.append(
            "Confirmer les parcelles retenues avec le plan cadastral ou un document opposable avant signature."
        )

    # 5) Projets voisins
    if priority_projects:
        actions.append(
            "Consulter les dossiers d’urbanisme des projets les plus proches avant décision définitive."
        )

    # 6) Climat / visite
    if climate_profile.get("available"):
        actions.append(
            "Lors de la visite, vérifier protections solaires, ventilation nocturne, gestion des eaux "
            "et état de la végétation."
        )

    # 7) Risque actuel orange/rouge
    if current_red or current_orange:
        actions.insert(
            0,
            "Faire confirmer les alertes actuelles orange/rouge par les documents officiels ou un professionnel compétent."
        )

    # Dédoublonnage.
    def dedupe(values):
        out=[];seen=set()
        for v in values:
            key=v.strip().lower()
            if key and key not in seen:
                seen.add(key);out.append(v)
        return out

    strengths=dedupe(strengths)[:6]
    watch=dedupe(watch)[:7]
    actions=dedupe(actions)[:7]

    if not strengths:
        strengths=["Aucun point rassurant suffisamment documenté pour être mis en avant."]
    if not watch:
        watch=["Aucun point de vigilance majeur identifié dans les données interprétées."]
    if not actions:
        actions=["Aucune action prioritaire supplémentaire générée par PRM."]

    return {
        "strengths":strengths,
        "watch":watch,
        "actions":actions,
    }

def pdf_safe(s):
    # ReportLab standard fonts handle Latin-1 well; normalize unsupported glyphs.
    return (str(s or "")
            .replace("•","-")
            .replace("→","-")
            .replace("≈","env.")
            .replace("≤","<=")
            .replace("≥",">=")
            .replace("’","'")
            .replace("“",'"')
            .replace("”",'"')
            .replace("–","-")
            .replace("—","-"))

def build_prm_pdf(geo, rows, gs, gl, headline_reason, cats,
                  context_lines, future_lines, verify_lines,
                  premium, priority_projects, climate_profile,
                  resolver_source, resolver_conf):
    buf=BytesIO()
    doc=SimpleDocTemplate(
        buf,pagesize=A4,
        rightMargin=16*mm,leftMargin=16*mm,
        topMargin=16*mm,bottomMargin=16*mm,
        title="Rapport PRM",
        author="Property Risk Management"
    )

    styles=getSampleStyleSheet()
    title=ParagraphStyle(
        "PRMTitle",parent=styles["Title"],fontName="Helvetica-Bold",
        fontSize=22,leading=26,textColor=colors.HexColor("#111827"),
        spaceAfter=7
    )
    h1=ParagraphStyle(
        "PRMH1",parent=styles["Heading1"],fontName="Helvetica-Bold",
        fontSize=14,leading=18,textColor=colors.HexColor("#111827"),
        spaceBefore=8,spaceAfter=6
    )
    h2=ParagraphStyle(
        "PRMH2",parent=styles["Heading2"],fontName="Helvetica-Bold",
        fontSize=11.5,leading=14,textColor=colors.HexColor("#374151"),
        spaceBefore=5,spaceAfter=4
    )
    body=ParagraphStyle(
        "PRMBody",parent=styles["BodyText"],fontName="Helvetica",
        fontSize=9.2,leading=13,textColor=colors.HexColor("#374151"),
        spaceAfter=4
    )
    small=ParagraphStyle(
        "PRMSmall",parent=body,fontSize=7.8,leading=10,
        textColor=colors.HexColor("#6B7280")
    )
    verdict=ParagraphStyle(
        "PRMVerdict",parent=styles["Heading1"],fontName="Helvetica-Bold",
        fontSize=16,leading=20,textColor=colors.HexColor(
            "#B91C1C" if gs=="🔴" else "#C2410C" if gs=="🟠" else "#15803D"
        ),spaceBefore=6,spaceAfter=4
    )

    story=[]
    story.append(Paragraph("PROPERTY INTELLIGENCE", small))
    story.append(Paragraph("Rapport d’analyse immobilière", title))
    story.append(Paragraph(pdf_safe(geo.get("label")), h1))
    parcel_txt=", ".join(r.get("Parcelle","") for r in rows)
    story.append(Paragraph(
        pdf_safe(f"Parcelle(s) : {parcel_txt} | Resolver : {resolver_source} | Confiance : {resolver_conf}"),
        small
    ))
    story.append(Paragraph(
        "Synthèse automatisée des données publiques et des éléments fournis par l’utilisateur, "
        "structurée pour préparer une décision immobilière.",
        small
    ))
    story.append(Spacer(1,5*mm))
    story.append(Paragraph(pdf_safe(gl), verdict))
    story.append(Paragraph(pdf_safe(headline_reason), body))
    story.append(Spacer(1,3*mm))

    # Executive summary boxes.
    cells=[]
    for head,key in [
        ("POINTS RASSURANTS","strengths"),
        ("A SURVEILLER","watch"),
        ("ACTIONS AVANT DECISION","actions"),
    ]:
        content=[Paragraph(head,h2)]
        for line in premium[key]:
            content.append(Paragraph(pdf_safe("- "+line), body))
        cells.append(content)

    t=Table([cells],colWidths=[58*mm,58*mm,58*mm],hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("BOX",(0,0),(-1,-1),0.6,colors.HexColor("#D1D5DB")),
        ("INNERGRID",(0,0),(-1,-1),0.4,colors.HexColor("#E5E7EB")),
        ("BACKGROUND",(0,0),(0,0),colors.HexColor("#F0FDF4")),
        ("BACKGROUND",(1,0),(1,0),colors.HexColor("#FFF7ED")),
        ("BACKGROUND",(2,0),(2,0),colors.HexColor("#F9FAFB")),
        ("LEFTPADDING",(0,0),(-1,-1),7),
        ("RIGHTPADDING",(0,0),(-1,-1),7),
        ("TOPPADDING",(0,0),(-1,-1),7),
        ("BOTTOMPADDING",(0,0),(-1,-1),7),
    ]))
    story.append(t)
    story.append(Spacer(1,5*mm))

    story.append(Paragraph("Risques actuels - détail", h1))
    risk_rows=[["Thématique","Lecture PRM","Niveau / confiance"]]
    for name,x in cats.items():
        if name in ["Urbanisme","Climat 2050"]:
            continue
        level=x.get("official_level") or plain_status(x.get("status"))
        conf=x.get("confidence") or "non précisée"
        risk_rows.append([
            Paragraph(pdf_safe(name),body),
            Paragraph(pdf_safe(x.get("label")),body),
            Paragraph(pdf_safe(f"{level} | {conf}"),body)
        ])
    rt=Table(risk_rows,colWidths=[46*mm,86*mm,42*mm],repeatRows=1)
    rt.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#111827")),
        ("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
        ("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#D1D5DB")),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("LEFTPADDING",(0,0),(-1,-1),5),
        ("RIGHTPADDING",(0,0),(-1,-1),5),
        ("TOPPADDING",(0,0),(-1,-1),5),
        ("BOTTOMPADDING",(0,0),(-1,-1),5),
    ]))
    story.append(rt)

    story.append(Paragraph("Contexte et contraintes", h1))
    for line in context_lines:
        story.append(Paragraph(pdf_safe("- "+line),body))

    story.append(Paragraph("Adaptation future", h1))
    for line in future_lines:
        story.append(Paragraph(pdf_safe("- "+line),body))

    if priority_projects:
        story.append(Paragraph("Projets voisins prioritaires", h1))
        for p in priority_projects[:5]:
            line=(
                f"{p.get('kind')} - {p.get('distance_m')} m - "
                f"{p.get('date') or 'date non interprétée'} - "
                f"{p.get('address') or 'adresse non extraite'}"
            )
            story.append(Paragraph(pdf_safe("- "+line),body))

    story.append(Paragraph("Vérifications avant décision", h1))
    for line in verify_lines[:7]:
        story.append(Paragraph(pdf_safe("- "+line),body))

    story.append(Spacer(1,5*mm))
    story.append(Paragraph(
        "Important : ce rapport est un outil d'aide à la décision construit à partir de données publiques, "
        "de traitements automatiques et, le cas échéant, de déclarations visuelles de l'utilisateur. "
        "Il ne constitue ni un diagnostic réglementaire, ni une garantie sur le bien. "
        "Les éléments importants doivent être confirmés auprès des administrations, professionnels compétents "
        "et documents opposables avant engagement.",
        small
    ))

    def footer(canvas,doc):
        canvas.saveState()
        canvas.setFont("Helvetica",7)
        canvas.setFillColor(colors.HexColor("#9CA3AF"))
        canvas.drawString(16*mm,8*mm,"PRM V1.0A - Rapport décisionnel")
        canvas.drawRightString(A4[0]-16*mm,8*mm,f"Page {doc.page}")
        canvas.restoreState()

    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    return buf.getvalue()


# ---------------- Climat 2050 ----------------
IDF_DEPTS = {"75","77","78","91","92","93","94","95"}

IDF_SUMMER_WARMING = {
    "75": 2.2, "77": 2.3, "78": 2.3, "91": 2.4,
    "92": 2.3, "93": 2.2, "94": 2.3, "95": 2.2
}
IDF_WINTER_WARMING = {
    "75": 1.6, "77": 1.8, "78": 1.7, "91": 1.7,
    "92": 1.6, "93": 1.7, "94": 1.7, "95": 1.7
}

def dept_from_postcode(postcode):
    s=str(postcode or "")
    if len(s)>=2:
        if s.startswith(("97","98")) and len(s)>=3:
            return s[:3]
        return s[:2]
    return None

def climate_2050_profile(geo):
    """
    Conservative climate module:
    - national TRACC anchor for metropolitan France;
    - detailed regional/departemental values only where an official
      Météo-France 2050 regional publication is hard-coded and sourced.
    """
    dept=dept_from_postcode(geo.get("postcode"))
    base={
        "horizon":"2050",
        "framework":"TRACC",
        "national_warming":"+2,7 °C France hexagonale et Corse vs préindustriel",
        "reference_period":"1976-2005 pour les comparaisons régionales Météo-France",
        "source":"Météo-France / TRACC",
        "scope":"région / département",
        "confidence":"élevée",
        "checked_at":now_fr(),
        "available":False,
        "region":None,
        "metrics":[]
    }

    if dept in IDF_DEPTS:
        base["available"]=True
        base["region"]="Île-de-France"
        summer=IDF_SUMMER_WARMING.get(dept)
        winter=IDF_WINTER_WARMING.get(dept)
        base["metrics"]=[
            {
                "name":"Température moyenne annuelle",
                "status":"🟠",
                "future":"+1,9 °C",
                "baseline":"par rapport à 1976-2005",
                "scope":"moyenne régionale",
                "why":"Climat globalement plus chaud ; confort d’été et besoins de rafraîchissement à anticiper."
            },
            {
                "name":"Température estivale",
                "status":"🟠",
                "future":f"+{summer:.1f} °C" if summer is not None else "+2,2 à +2,4 °C",
                "baseline":"par rapport à 1976-2005",
                "scope":f"département {dept}" if summer is not None else "Île-de-France",
                "why":"Impact direct sur le confort d’été, les combles, les façades exposées et les besoins d’ombrage."
            },
            {
                "name":"Température hivernale",
                "status":"🟢",
                "future":f"+{winter:.1f} °C" if winter is not None else "+1,6 à +1,8 °C",
                "baseline":"par rapport à 1976-2005",
                "scope":f"département {dept}" if winter is not None else "Île-de-France",
                "why":"Hivers plus doux en moyenne ; baisse possible de certains besoins de chauffage."
            },
            {
                "name":"Jours > 35 °C",
                "status":"🟠",
                "future":"≈ 4 jours/an",
                "baseline":"< 1 jour/an dans le climat passé",
                "scope":"moyenne régionale",
                "why":"Les pics de chaleur deviennent un enjeu réel pour les logements peu protégés du soleil."
            },
            {
                "name":"Nuits chaudes > 20 °C",
                "status":"🟠",
                "future":"≈ 10 nuits/an",
                "baseline":"≈ 2 nuits/an dans le climat passé",
                "scope":"moyenne régionale",
                "why":"Rafraîchissement nocturne plus difficile ; attention aux chambres sous toiture et aux logements traversants ou non."
            },
            {
                "name":"Sécheresse des sols",
                "status":"🟠",
                "future":"≈ 139 jours/an",
                "baseline":"≈ 118 jours/an",
                "scope":"moyenne régionale",
                "why":"Stress accru pour jardins et végétation ; à croiser avec le risque argiles et la gestion de l’eau."
            },
            {
                "name":"Pluies les plus intenses",
                "status":"🟠",
                "future":"≈ +10 %",
                "baseline":"intensité des épisodes forts",
                "scope":"moyenne régionale",
                "why":"Peut accroître ruissellement, saturation des réseaux et vulnérabilité des sous-sols selon la topographie locale."
            },
            {
                "name":"Jours à risque élevé de feu",
                "status":"🟠",
                "future":"≈ 7 jours/an",
                "baseline":"≈ 1,6 jour/an",
                "scope":"moyenne régionale",
                "why":"La végétation autour du bien devient plus importante à surveiller ; exposition réelle à confirmer par photos et contexte local."
            },
        ]
    else:
        base["metrics"]=[
            {
                "name":"Trajectoire 2050",
                "status":"⚪",
                "future":"+2,7 °C France hexagonale et Corse",
                "baseline":"par rapport à l’ère préindustrielle",
                "scope":"national",
                "why":"PRM n’a pas encore branché la projection régionale détaillée pour ce territoire."
            }
        ]
    return base

def climate_summary_status(profile):
    if not profile.get("available"):
        return "⚪","Données locales à compléter"
    oranges=sum(m.get("status")=="🟠" for m in profile.get("metrics",[]))
    return ("🟠","Adaptation à anticiper") if oranges else ("🟢","Évolution limitée")

def climate_card(metric):
    st.markdown(f"""
    <div style="border:1px solid #e5e7eb;border-radius:14px;padding:15px 17px;margin:7px 0;background:#fff">
      <div style="font-size:1.02rem;font-weight:750">{metric['status']} {metric['name']}</div>
      <div style="font-size:1.15rem;font-weight:700;margin-top:5px">{metric['future']}</div>
      <div style="color:#6b7280;margin-top:2px">{metric['baseline']}</div>
      <div style="color:#374151;margin-top:8px">{metric['why']}</div>
      <div style="font-size:.77rem;color:#9ca3af;margin-top:9px">Portée : {metric['scope']}</div>
    </div>
    """, unsafe_allow_html=True)

# ---------------- Sitadel ----------------
def haversine(lat1,lon1,lat2,lon2):
    R=6371000;p1,p2=math.radians(lat1),math.radians(lat2)
    dp=math.radians(lat2-lat1);dl=math.radians(lon2-lon1)
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R*2*math.atan2(math.sqrt(a),math.sqrt(1-a))

def parse_float(v):
    try:return float(str(v).replace(",","."))
    except:return None

def extract_coords(rec):
    lat=lon=None
    for k,v in rec.items():
        lk=str(k).lower()
        if lk in ["latitude","lat"] or "latitude" in lk:lat=parse_float(v)
        if lk in ["longitude","lon","lng"] or "longitude" in lk:lon=parse_float(v)
    if lat is not None and lon is not None and -90<=lat<=90 and -180<=lon<=180:return lat,lon
    for key in ["_geopoint","geopoint","geo_point"]:
        v=rec.get(key)
        if isinstance(v,dict):
            la=parse_float(v.get("lat"));lo=parse_float(v.get("lon"))
            if la is not None and lo is not None:return la,lo
        if isinstance(v,str):
            nums=re.findall(r"-?\d+(?:\.\d+)?",v)
            if len(nums)>=2:
                a,b=map(float,nums[:2])
                if 40<a<52 and -6<b<11:return a,b
                if 40<b<52 and -6<a<11:return b,a
    return None,None

def parse_date_any(v):
    if not v:return None
    s=str(v).strip()
    for fmt in ["%Y-%m-%d","%d/%m/%Y","%Y/%m/%d","%d-%m-%Y","%Y-%m-%dT%H:%M:%S"]:
        try:return datetime.strptime(s[:19],fmt)
        except:pass
    m=re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})",s)
    if m:
        try:return datetime(int(m.group(1)),int(m.group(2)),int(m.group(3)))
        except:pass
    m=re.search(r"(\d{1,2})[/.-](\d{1,2})[/.-](20\d{2})",s)
    if m:
        try:return datetime(int(m.group(3)),int(m.group(2)),int(m.group(1)))
        except:pass
    return None

def best_date(rec):
    priorities=["date_reelle_autorisation","date_autorisation","date_decision","date_accord","date_depot","date"]
    for p in priorities:
        for k,v in rec.items():
            if p in str(k).lower():
                d=parse_date_any(v)
                if d:return d,str(k)
    return None,None

def value_by_keys(rec,keys):
    for wanted in keys:
        for k,v in rec.items():
            if wanted == str(k).lower() and v not in [None,""]:
                return v
    return None

def value_contains(rec,tokens):
    for k,v in rec.items():
        lk=str(k).lower()
        if any(t in lk for t in tokens) and v not in [None,"",[]]:
            return v
    return None

def clean_piece(v):
    if v is None:return None
    s=" ".join(str(v).strip().split())
    if not s or s.lower() in ["nan","none","null","0"]:return None
    return s

def project_address(rec):
    """
    Reconstruit une adresse lisible et écarte les identifiants techniques
    que certains jeux Sitadel font remonter dans des champs voisins.
    """
    def valid_address_piece(v):
        s=clean_piece(v)
        if not s:return None
        # Reject technical identifiers such as 200054781, parcel/internal IDs, etc.
        if re.fullmatch(r"\d{6,}",s):return None
        if re.fullmatch(r"[0-9A-Z]{8,}",s.upper()) and not re.search(r"[ÉÈÀA-Z]{3,}\s",s.upper()):
            return None
        # Generic intercommunal labels are not street localities.
        if s.lower() in ["le grand paris","métropole du grand paris","metropole du grand paris"]:
            return None
        return s

    direct_keys=["adresse_terrain","adresse","adr_terrain","adresse_travaux","adresse_projet"]
    for key in direct_keys:
        v=valid_address_piece(value_by_keys(rec,[key]))
        if v and not re.fullmatch(r"\d{5}",v):
            return v

    num=valid_address_piece(value_contains(rec,["num_voie","numero_voie","numvoie","num_terrain"]))
    typ=valid_address_piece(value_contains(rec,["type_voie","nature_voie"]))
    voie=valid_address_piece(value_contains(rec,["nom_voie","libelle_voie","voie_terrain","voie"]))
    commune=valid_address_piece(value_contains(rec,["nom_commune","commune"]))
    cp=valid_address_piece(value_contains(rec,["code_postal","cp_terrain"]))

    # Postal code is accepted only as postal code.
    if cp and not re.fullmatch(r"\d{5}",cp):
        cp=None

    street=[x for x in [num,typ,voie] if x]
    locality=" ".join([x for x in [cp,commune] if x])
    if street:
        return " ".join(street)+(f", {locality}" if locality else "")
    return locality or None

def project_reference(rec):
    priorities=["num_dau","numero_dau","num_autorisation","numero_autorisation","num_pc","num_pa","num_pd"]
    for p in priorities:
        v=clean_piece(value_by_keys(rec,[p]))
        if v:return v
    for k,v in rec.items():
        if ("num" in str(k).lower() or "id" == str(k).lower()) and v not in [None,""]:
            s=clean_piece(v)
            if s and len(s)>6:return s
    return None

def code_label(code,category):
    c=(clean_piece(code) or "").upper()
    labels={"PC":"Permis de construire","DP":"Déclaration préalable","PA":"Permis d’aménager","PD":"Permis de démolir"}
    if c in labels:return labels[c]
    if "Permis de démolir" in category:return "Permis de démolir"
    if "Permis d’aménager" in category:return "Permis d’aménager"
    return category

def project_kind(rec,category):
    code=value_contains(rec,["type_dau","nature_dau","type_autorisation","nature_autorisation"])
    code=clean_piece(code)
    return code_label(code,category)

def project_description(rec,category):
    """
    Traduit seulement les codes dont la signification est suffisamment
    documentée. Sinon PRM affiche un libellé générique plutôt que d'inventer.
    """
    pieces=[]

    nature_raw=clean_piece(value_contains(rec,["nature_projet"]))
    nature_map={
        "1":"Nouvelle construction",
        "2":"Travaux sur construction existante",
    }
    if nature_raw:
        pieces.append("Nature : "+nature_map.get(nature_raw, f"Code Sitadel {nature_raw}"))

    # The dataset itself already tells us the broad destination reliably.
    if "Logements" in category:
        pieces.append("Destination : habitation / logement")
    elif "Locaux" in category:
        pieces.append("Destination : local non résidentiel")

    nb=clean_piece(value_contains(rec,["nb_logements","nombre_logements","nb_lgt"]))
    if nb and nb not in ["0","0.0"]:
        pieces.append(f"Logements créés : {nb}")

    surf=clean_piece(value_contains(rec,["surface_creee","surf_creee","surface_plancher_creee"]))
    if surf and surf not in ["0","0.0"]:
        if re.fullmatch(r"\d+(?:[.,]\d+)?",surf):
            surf=f"{surf} m²"
        pieces.append(f"Surface créée : {surf}")

    # Explicit textual destinations are useful; raw numeric codes are not shown.
    dest=clean_piece(value_contains(rec,["libelle_destination","destination_libelle","nom_destination"]))
    if dest and not re.fullmatch(r"\d+",dest):
        pieces.append(f"Destination détaillée : {dest}")

    if not pieces:
        return project_kind(rec,category)

    return " · ".join(pieces[:4])

def query_sitadel_dataset(dataset_id,citycode,city):
    url=f"{DATAFAIR_BASE}/{dataset_id}/lines"
    attempts=[{"size":1000,"qs":f"COMM:{citycode}"},{"size":1000,"q":city or citycode}]
    errors=[]
    for params in attempts:
        data,err=api_get(url,params)
        if not err and data:
            results=data.get("results",[]) if isinstance(data,dict) else []
            if results:return results,None
        errors.append(err)
    return []," ; ".join(str(x) for x in errors if x)

def get_projects(lat,lon,citycode,city,radius=500):
    out=[];errors={}
    for category,dataset in SITADEL_DATASETS.items():
        records,err=query_sitadel_dataset(dataset,citycode,city)
        if err:errors[dataset]=err
        for rec in records:
            plat,plon=extract_coords(rec)
            if plat is None or plon is None:continue
            dist=haversine(lat,lon,plat,plon)
            if dist>radius:continue
            dt,_=best_date(rec)
            year=dt.year if dt else None
            recent=bool(dt and dt.year>=2021)
            very_recent=bool(dt and dt.year>=2024)
            kind=project_kind(rec,category)
            priority=(0 if very_recent and dist<=250 else 1 if recent and dist<=250 else 2 if dist<=250 else 3 if recent else 4)
            out.append({
                "category":category,"kind":kind,"dataset":dataset,
                "distance_m":round(dist),"date":dt.strftime("%d/%m/%Y") if dt else None,
                "date_sort":dt.timestamp() if dt else 0,"year":year,
                "recent":recent,"very_recent":very_recent,"priority":priority,
                "address":project_address(rec),"reference":project_reference(rec),
                "description":project_description(rec,category),
                "lat":plat,"lon":plon,
            })
    dedup=[];seen=set()
    for p in sorted(out,key=lambda x:(x["priority"],x["distance_m"],-x["date_sort"])):
        key=p["reference"] or (p["kind"],p["address"],p["date"],p["distance_m"]//10)
        if key in seen:continue
        seen.add(key);dedup.append(p)
    return dedup,errors

def project_score_label(p):
    if p["very_recent"] and p["distance_m"]<=250:return "À regarder en priorité","🟠"
    if p["recent"] and p["distance_m"]<=250:return "Projet récent proche","🟠"
    if p["distance_m"]<=100:return "Très proche, mais ancien/date inconnue","⚪"
    return "Contexte du quartier","⚪"

def row(f,candidates):
    p=f.get("properties",{});dist=None
    for d,cf in candidates:
        if parcel_id(cf)==parcel_id(f):dist=round(d,1);break
    return {"Parcelle":parcel_id(f),"Surface m²":p.get("contenance"),"Distance approx.":dist,"ID cadastral":p.get("idu") or p.get("id")}

def card(name,x):
    lvl=f" · Niveau : {x['official_level']}" if x.get("official_level") else ""
    details="".join(f"<div style='margin-top:4px;color:#4b5563'>• {d}</div>" for d in x.get("details",[]))
    st.markdown(f"""<div style="border:1px solid #e5e7eb;border-radius:14px;padding:15px 17px;margin:7px 0;background:#fff">
    <div style="font-size:1.05rem;font-weight:750">{x['status']} {name}</div><div style="color:#374151;margin-top:5px">{x['label']}</div>{details}
    <div style="font-size:.77rem;color:#9ca3af;margin-top:9px">Source : {x['source']}{lvl}<br>Portée : {x['scope']} · Confiance : {x['confidence']}<br>Vérifié : {x['checked_at']}</div></div>""",unsafe_allow_html=True)


# ---------------- Inspection visuelle végétation / feu ----------------
VEG_ITEMS = [
    ("branches_toit", "Branches en contact ou très proches de la toiture / gouttières", 3,
     "Élagage / éloignement à vérifier ; accumulation de feuilles et continuité vers le toit."),
    ("vegetation_facade", "Végétation dense collée à une façade ou sous des ouvertures", 2,
     "Créer une zone de séparation et éviter la continuité directe vers le bâti."),
    ("dry_grass", "Herbes, feuilles ou végétation sèche visibles près du bâti", 3,
     "Nettoyer les matières sèches et maintenir les abords immédiats."),
    ("fuel_storage", "Bois, palettes, cartons ou combustibles stockés près de la maison", 3,
     "Éloigner les stocks combustibles de la façade."),
    ("hedge_continuity", "Haie / végétation continue reliant l’extérieur à la maison", 2,
     "Rompre la continuité végétale à proximité immédiate du bâti."),
    ("conifer", "Résineux / végétation très inflammable très proche du bâti", 2,
     "Évaluer la distance au bâti et l’entretien."),
    ("roof_debris", "Feuilles / aiguilles / débris visibles sur toiture ou gouttières", 3,
     "Nettoyage régulier des toitures et gouttières."),
    ("access", "Accès engins / voie d’approche semblant étroit ou encombré", 1,
     "À confirmer sur place ; préserver un accès dégagé."),
]

def vegetation_assessment(selected_keys, photo_count):
    points=0
    findings=[]
    lookup={k:(label,w,advice) for k,label,w,advice in VEG_ITEMS}
    for key in selected_keys:
        if key in lookup:
            label,w,advice=lookup[key]
            points+=w
            findings.append({"key":key,"label":label,"weight":w,"advice":advice})

    if photo_count == 0:
        return {
            "status":"⚪","label":"Photos requises","score":None,
            "confidence":"aucune","findings":[],
            "note":"Aucune photo fournie."
        }

    if not selected_keys:
        return {
            "status":"🟢","label":"Aucun facteur visuel déclaré sur les photos fournies","score":0,
            "confidence":"faible" if photo_count < 3 else "moyenne","findings":[],
            "note":"Ce résultat dépend uniquement des photos fournies et des éléments cochés."
        }

    if points >= 8:
        status,label="🔴","Vulnérabilité visuelle élevée à vérifier"
    elif points >= 4:
        status,label="🟠","Vigilance visuelle"
    else:
        status,label="🟠","Quelques facteurs visuels à vérifier"

    conf="faible" if photo_count < 3 else ("moyenne" if photo_count < 5 else "élevée")
    return {
        "status":status,"label":label,"score":points,
        "confidence":conf,"findings":findings,
        "note":"Évaluation visuelle indicative, non réglementaire."
    }

st.markdown('<div style="font-size:.8rem;letter-spacing:.12em;text-transform:uppercase;color:#9ca3af">Property Risk Management · Prototype V1.0A</div>',unsafe_allow_html=True)
st.title("Avant d’acheter, voyez les risques.")
st.write("V1.0A affine l’expérience produit, le parcours utilisateur et la restitution PDF sans figer la marque finale.")


st.subheader("📷 Compléter avec des photos — optionnel")
st.caption(
    "Ajoutez idéalement 3 à 6 photos si vous souhaitez compléter l’analyse visuelle : façade/jardin, toiture-gouttières, limites du terrain et végétation proche. "
    "V0.6 ne fait pas de reconnaissance automatique : vous confirmez les éléments visibles dans la grille après l’analyse."
)
veg_photos = st.file_uploader(
    "Photos de la propriété",
    type=["jpg","jpeg","png","webp"],
    accept_multiple_files=True,
    help="Les photos servent de preuves visuelles dans le snapshot PRM."
)
if veg_photos:
    preview_cols=st.columns(min(3,len(veg_photos)))
    for i,p in enumerate(veg_photos[:6]):
        with preview_cols[i % len(preview_cols)]:
            st.image(p,caption=p.name,use_container_width=True)


st.info(
    "PRM distingue volontairement ce qui est **mesuré/interprété**, ce qui relève du **contexte**, "
    "et ce qui reste **à confirmer**. L’objectif est d’éviter les faux signaux de sécurité comme les fausses alertes."
)

with st.form("search"):
    address=st.text_input("Adresse",value="27 rue des Jardins 92380 Garches")
    radius=st.select_slider("Rayon projets voisins",options=[100,250,500],value=500,format_func=lambda x:f"{x} m")
    go=st.form_submit_button("Analyser cette propriété",use_container_width=True)

if go:
    st.session_state["prm_analysis_active"] = True
    st.session_state["prm_analysis_address"] = address
    st.session_state["prm_analysis_radius"] = radius

analysis_active = st.session_state.get("prm_analysis_active", False)

if analysis_active:
    run_address = st.session_state.get("prm_analysis_address", address)
    run_radius = st.session_state.get("prm_analysis_radius", radius)

    with st.spinner("Identification, risques, urbanisme et projets voisins…"):
        geo,gerr=geocode(run_address)
        if not geo:st.error(gerr);st.stop()
        selected,reasons,resolver_conf,resolver_source,resolver_errors,candidates=auto_resolve(run_address,geo["lon"],geo["lat"])
        if not selected:st.error("PRM n’a pas pu identifier de parcelle probable.");st.stop()
        geom=union_geom(selected)
        urbanism,uerrors=get_urbanism(geom)
        reports,rerrs=get_reports_stable(geo["lon"],geo["lat"])
        pdf_text,pdf_error=get_georisques_pdf_text(geo["lon"],geo["lat"])
        projects,project_errors=get_projects(geo["lat"],geo["lon"],geo.get("citycode"),geo.get("city"),run_radius)
        climate_profile=climate_2050_profile(geo)

    rows=[row(f,candidates) for f in selected];zones=zone_labels(urbanism.get("zones",[]))
    n_presc=sum(len(urbanism.get(k,[])) for k in ["prescriptions_surface","prescriptions_line","prescriptions_point","infos_surface"])
    stable_risks=stable_current_risks(reports)

    # V0.7.3 — use the official generated PDF as a fallback for regulatory scales.
    radon_pdf=classify_radon_text(pdf_text)
    seisme_pdf=classify_seisme_text(pdf_text)
    tech_pdf=classify_tech_pdf(pdf_text)

    if radon_pdf:
        stable_risks["Radon"]=radon_pdf
    else:
        stable_risks["Radon"]["details"].append(
            "Rapport PDF officiel consulté, mais échelle radon non extraite automatiquement."
        )

    if seisme_pdf:
        stable_risks["Séisme"]=seisme_pdf
    else:
        stable_risks["Séisme"]["details"].append(
            "Rapport PDF officiel consulté, mais zonage sismique non extrait automatiquement."
        )

    if tech_pdf:
        stable_risks["Risques technologiques"]=tech_pdf
    else:
        stable_risks["Risques technologiques"]["details"].append(
            "Rapport PDF officiel consulté : aucune distance technologique explicite supplémentaire extraite."
        )

    # V0.7.1 — read persisted checkbox state before building the main PRM cards.
    # This allows the vegetation result to update immediately after any checkbox interaction.
    selected_veg_state = [
        key for key, label, weight, advice in VEG_ITEMS
        if st.session_state.get(f"veg_{key}", False)
    ]
    veg_assessment = vegetation_assessment(
        selected_veg_state,
        len(veg_photos or [])
    )

    veg_details = []
    if veg_assessment.get("score") is not None:
        veg_details.append(
            f"Score visuel interne : {veg_assessment['score']} · "
            f"{len(veg_photos or [])} photo(s)"
        )
    if veg_assessment.get("findings"):
        veg_details.extend([f["label"] for f in veg_assessment["findings"][:3]])

    vegetation_card = item(
        veg_assessment["status"],
        veg_assessment["label"],
        source="Photos utilisateur",
        scope="propriété",
        confidence=veg_assessment["confidence"],
        details=veg_details
    )

    cats={
        "Inondation / nappe":stable_risks["Inondation / nappe"],
        "Argiles / sols":stable_risks["Argiles / sols"],
        "Mouvements / cavités":stable_risks["Mouvements / cavités"],
        "Séisme":stable_risks["Séisme"],
        "Radon":stable_risks["Radon"],
        "Risques technologiques":stable_risks["Risques technologiques"],
        "Urbanisme":item("🟠" if zones else "⚪","Zonage détecté : "+", ".join(zones[:3]) if zones else "Zonage non récupéré",source="GPU / IGN",scope="parcelles",confidence="élevée" if zones else "faible",official_level=", ".join(zones[:3]) if zones else None,details=[f"{n_presc} prescription(s)/information(s) GPU intersectante(s)"]),
        "Climat 2050":item("🟠" if climate_profile.get("available") else "⚪","Adaptation à anticiper" if climate_profile.get("available") else "Données locales à compléter",source="Météo-France / TRACC",scope="région / département",confidence="élevée" if climate_profile.get("available") else "moyenne"),
        "Végétation / vulnérabilité feu":vegetation_card
    }
    # ---------------- V0.8 — moteur de synthèse décisionnelle ----------------
    current_names=[
        "Inondation / nappe","Argiles / sols","Mouvements / cavités",
        "Séisme","Radon","Risques technologiques","Végétation / vulnérabilité feu"
    ]
    current_items={k:cats[k] for k in current_names}

    current_red=[k for k,v in current_items.items() if v["status"]=="🔴"]
    current_orange=[k for k,v in current_items.items() if v["status"]=="🟠"]
    current_gray=[k for k,v in current_items.items() if v["status"]=="⚪"]

    # Le bandeau est désormais piloté uniquement par les risques actuels.
    if current_red:
        gs,gl="🔴","VIGILANCE FORTE"
        headline_reason=f"{len(current_red)} risque(s) actuel(s) en niveau rouge."
    elif current_orange:
        gs,gl="🟠","VIGILANCE"
        headline_reason=f"{len(current_orange)} point(s) de risque actuel à examiner."
    elif current_gray:
        gs,gl="🟢","AUCUNE ALERTE MAJEURE DÉTECTÉE"
        headline_reason=f"Aucune alerte rouge/orange sur les risques interprétés · {len(current_gray)} point(s) restent à compléter."
    else:
        gs,gl="🟢","AUCUNE ALERTE MAJEURE DÉTECTÉE"
        headline_reason="Aucune alerte rouge/orange détectée sur les risques actuels analysés."

    priority_projects=[p for p in projects if p["priority"]<=1]

    context_lines=[]
    if zones:
        context_lines.append(f"Urbanisme : zonage {', '.join(zones[:3])}.")
    else:
        context_lines.append("Urbanisme : zonage non récupéré.")

    if n_presc:
        context_lines.append(f"{n_presc} prescription(s) / information(s) GPU intersectante(s).")
    else:
        context_lines.append("Aucune prescription/information GPU particulière détectée sur les parcelles.")

    if priority_projects:
        nearest=min(priority_projects,key=lambda p:p["distance_m"])
        context_lines.append(
            f"{len(priority_projects)} autorisation(s) récente(s) prioritaire(s) dans le quartier ; "
            f"la plus proche est à {nearest['distance_m']} m."
        )
    else:
        context_lines.append("Aucune autorisation récente prioritaire détectée dans le rayon analysé.")

    future_lines=[]
    if climate_profile.get("available"):
        future_lines.append("Climat 2050 : adaptation à anticiper, sans assimilation à un risque actuel au numéro de rue.")
        future_lines.append("Priorités : confort d’été, gestion de l’eau, pluies intenses et végétation.")
    else:
        future_lines.append("Climat 2050 : données territoriales locales à compléter.")

    verify_lines=[]
    for name in current_gray:
        verify_lines.append(f"{name} : {current_items[name]['label']}.")
    if resolver_conf!="élevée":
        verify_lines.append(f"Parcelles : resolver {resolver_source}, confiance {resolver_conf}.")
    if not veg_photos and not any(line.startswith("Végétation / vulnérabilité feu") for line in verify_lines):
        verify_lines.append("Végétation / feu : ajouter des photos pour une inspection visuelle.")
    # Déduplication prudente des points à vérifier, en conservant l'ordre.
    deduped_verify=[]
    seen_verify=set()
    for line in verify_lines:
        key=line.strip().lower()
        if key not in seen_verify:
            seen_verify.add(key)
            deduped_verify.append(line)
    verify_lines=deduped_verify

    if not verify_lines:
        verify_lines.append("Aucun point majeur non résolu dans les données actuellement interprétées.")

    st.markdown(f"""<div style="padding:24px;border-radius:20px;background:#111827;color:white;margin-bottom:18px">
    <div style="font-size:.85rem;color:#9ca3af">PRM SNAPSHOT V1.0A</div>
    <div style="font-size:1.55rem;font-weight:800;margin-top:5px">{geo['label']}</div>
    <div style="font-size:1rem;color:#d1d5db;margin-top:4px">Parcelle(s) : {", ".join(x["Parcelle"] for x in rows)}</div>
    <div style="font-size:.9rem;color:#9ca3af;margin-top:3px">Resolver : {resolver_source} · Confiance : {resolver_conf}</div>
    <div style="font-size:2.1rem;font-weight:800;margin-top:14px">{gs} {gl}</div>
    <div style="font-size:.95rem;color:#d1d5db;margin-top:6px">{headline_reason}</div>
    </div>""",unsafe_allow_html=True)

    st.subheader("🧭 Synthèse décisionnelle")
    s1,s2=st.columns(2)

    with s1:
        st.markdown("### Risques actuels")
        if current_red:
            st.error("Alerte(s) forte(s) : "+", ".join(current_red))
        elif current_orange:
            st.warning("Point(s) de vigilance : "+", ".join(current_orange))
        else:
            st.success("Aucune alerte rouge/orange sur les risques actuels interprétés.")
        if current_gray:
            st.caption("Données encore incomplètes : "+", ".join(current_gray))

        st.markdown("### Contraintes & contexte")
        for line in context_lines:
            st.write("• "+line)

    with s2:
        st.markdown("### Adaptation future")
        for line in future_lines:
            st.write("• "+line)

        st.markdown("### Points à vérifier")
        for line in verify_lines[:6]:
            st.write("• "+line)

    st.caption(
        "Lecture PRM : le bandeau global est désormais piloté par les risques actuels. "
        "Urbanisme, projets voisins et climat 2050 sont présentés séparément afin de ne pas les confondre avec une exposition actuelle."
    )

    premium=premium_summary(
        cats,current_red,current_orange,current_gray,
        resolver_source,resolver_conf,priority_projects,
        climate_profile,verify_lines
    )

    st.subheader("📋 Lecture premium PRM")
    p1,p2,p3=st.columns(3)
    with p1:
        st.markdown("### Points rassurants")
        for line in premium["strengths"]:
            st.write("• "+line)
    with p2:
        st.markdown("### À surveiller")
        for line in premium["watch"]:
            st.write("• "+line)
    with p3:
        st.markdown("### Actions avant décision")
        for line in premium["actions"]:
            st.write("• "+line)

    st.caption(
        "Cette lecture hiérarchise le dossier ; elle ne transforme pas les données PRM en certification du bien."
    )

    c1,c2=st.columns(2)
    with c1:
        st.subheader("Localisation");st.map(pd.DataFrame([{"lat":geo["lat"],"lon":geo["lon"]}]),zoom=17,size=12)
    with c2:
        st.subheader("Propriété identifiée");st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)

    st.caption(
        "Moteur de stabilité Géorisques : chaque risque est lu jusqu’à 3 fois. "
        "PRM n’affiche un niveau que si une majorité des lectures concorde ; sinon le feu reste gris."
    )

    st.subheader("Feux PRM — avec preuves")
    cols=st.columns(2)
    for i,(name,x) in enumerate(cats.items()):
        with cols[i%2]:card(name,x)


    st.subheader("🌿 Végétation / vulnérabilité feu — inspection visuelle")
    st.write(
        "Cette section ne remplace pas un diagnostic réglementaire. "
        "Elle documente uniquement des facteurs visibles sur les photos fournies."
    )

    selected_veg=[]
    if veg_photos:
        st.caption(f"{len(veg_photos)} photo(s) fournie(s). Cochez uniquement ce que vous voyez réellement.")
        for key,label,weight,advice in VEG_ITEMS:
            if st.checkbox(label,key=f"veg_{key}"):
                selected_veg.append(key)
    else:
        st.info("Ajoutez des photos en haut de la page pour activer l’inspection visuelle.")

    # The assessment displayed here is recomputed from the current widget values.
    # On checkbox interaction Streamlit reruns, and the main PRM card above then
    # receives this same persisted state.
    veg_assessment=vegetation_assessment(selected_veg,len(veg_photos or []))

    st.markdown(f"### {veg_assessment['status']} {veg_assessment['label']}")
    if veg_assessment["score"] is not None:
        st.caption(
            f"Score visuel interne : {veg_assessment['score']} · "
            f"Confiance : {veg_assessment['confidence']} · "
            f"Photos : {len(veg_photos or [])}"
        )

    if veg_assessment["findings"]:
        for f in veg_assessment["findings"]:
            st.markdown(f"**• {f['label']}**")
            st.write(f"À vérifier / action possible : {f['advice']}")
    elif veg_photos:
        st.write("Aucun facteur de vulnérabilité n’a été coché sur les photos fournies.")

    st.caption(
        "La vulnérabilité réelle dépend aussi de la végétation hors champ, du vent, de la pente, "
        "des matériaux du bâtiment, de l’entretien et des obligations locales de débroussaillement."
    )

    st.caption("V0.7.1 : ce résultat est également repris dans la carte principale « Végétation / vulnérabilité feu ».")


    st.subheader("🏗 Projets & autorisations autour du bien")
    st.caption(f"Sitadel dans un rayon de {run_radius} m. Les autorisations sont diffusées mensuellement et peuvent comporter un délai de remontée.")

    if not projects:
        st.info("Aucune autorisation géolocalisée détectée dans ce rayon.")
    else:
        if priority_projects:
            st.markdown("### À regarder en priorité")
            for p in priority_projects[:5]:
                label,icon=project_score_label(p)
                st.markdown(f"**{icon} {p['kind']} — {p['distance_m']} m**")
                cols=st.columns([1,1])
                with cols[0]:
                    st.write("**Date :**",p["date"] or "Non interprétée")
                    st.write("**Adresse :**",p["address"] or "Adresse non extraite")
                with cols[1]:
                    st.write("**Référence :**",p["reference"] or "Non extraite")
                    st.write("**Lecture PRM :**",label)
                st.write("**Projet :**",p["description"])
                st.caption(f"Source : Sitadel / SDES via {p['dataset']}")
                st.divider()
        else:
            st.info("Aucun projet récent à moins de 250 m détecté dans les jeux interrogés.")

        history=[p for p in projects if p not in priority_projects]
        with st.expander(f"Voir l’historique du quartier — {len(history)} autre(s) autorisation(s)"):
            for p in history[:30]:
                label,icon=project_score_label(p)
                st.write(f"{icon} **{p['kind']} — {p['distance_m']} m** · {p['date'] or 'date inconnue'} · {p['address'] or 'adresse non extraite'}")

        map_rows=[{"lat":geo["lat"],"lon":geo["lon"],"size":18,"color":"#111827","label":"Bien analysé"}]
        for p in projects[:50]:
            _,icon=project_score_label(p)
            map_rows.append({
                "lat":p["lat"],"lon":p["lon"],
                "size":7 if icon=="🟠" else 4,
                "color":"#f97316" if icon=="🟠" else "#9ca3af",
                "label":p["kind"]
            })
        st.markdown("### Carte des projets")
        st.map(pd.DataFrame(map_rows),latitude="lat",longitude="lon",size="size",color="color",zoom=15,height=450)


    st.subheader("🌡️ Votre maison en 2050")
    cstatus,clabel=climate_summary_status(climate_profile)
    st.markdown(f"### {cstatus} {clabel}")
    st.caption(
        f"Horizon {climate_profile['horizon']} · Cadre {climate_profile['framework']} · "
        f"Source : {climate_profile['source']} · Portée : {climate_profile['scope']}"
    )
    st.write(
        "PRM présente ici l’évolution du climat du territoire, pas une prédiction météo de la parcelle. "
        "La vulnérabilité réelle du bâtiment dépend ensuite de son orientation, de l’isolation, des protections solaires, "
        "du sous-sol, de la végétation et de la topographie."
    )

    if climate_profile.get("region"):
        st.write(f"**Territoire climatique utilisé :** {climate_profile['region']}")
        st.write(f"**Référence :** {climate_profile['reference_period']}")
    else:
        st.info("La projection régionale détaillée n’est pas encore branchée pour ce territoire ; PRM conserve seulement la trajectoire nationale.")

    ccols=st.columns(2)
    for i,m in enumerate(climate_profile.get("metrics",[])):
        with ccols[i%2]:
            climate_card(m)

    if climate_profile.get("available"):
        st.markdown("#### Lecture immobilière PRM")
        st.write(
            "À l’horizon 2050, la priorité n’est pas seulement la température moyenne : pour un acheteur, "
            "les points les plus concrets sont le **confort d’été**, la **capacité à rafraîchir la maison la nuit**, "
            "la **gestion de l’eau et du jardin**, la **résilience aux pluies intenses** et la **végétation autour du bien**."
        )
        st.caption(
            "Ces valeurs sont des moyennes régionales/départementales Météo-France. "
            "Elles ne doivent pas être interprétées comme une mesure au numéro de rue."
        )


    st.subheader("Urbanisme parcellaire")
    st.write("**Zonage GPU :**"," · ".join(zones[:8]) if zones else "Non récupéré")
    st.write("Aucune prescription/information GPU particulière détectée sur le périmètre analysé." if n_presc==0 else f"{n_presc} prescription(s) ou information(s) GPU intersectent le périmètre.")

    snapshot={"generated_at":datetime.now(timezone.utc).isoformat(),"address":geo,"property_resolver":{"source":resolver_source,"confidence":resolver_conf,"parcels":rows},"risks":cats,"risk_engine":{"georisques_reads":len(reports),"errors":rerrs},
              "decision_summary":{
                  "headline":{"status":gs,"label":gl,"reason":headline_reason},
                  "current_red":current_red,
                  "current_orange":current_orange,
                  "current_gray":current_gray,
                  "context":context_lines,
                  "future":future_lines,
                  "to_verify":verify_lines,
                  "premium":premium
              },"projects":{"radius_m":run_radius,"priority":priority_projects,"all":projects,"errors":project_errors},"climate_2050":climate_profile,"vegetation_visual":veg_assessment,"georisques_pdf":{"available":bool(pdf_text),"error":pdf_error},"urbanism":{"zones":zones,"prescription_count":n_presc}}

    st.subheader("📄 Votre rapport décisionnel")
    report_pdf=build_prm_pdf(
        geo,rows,gs,gl,headline_reason,cats,
        context_lines,future_lines,verify_lines,
        premium,priority_projects,climate_profile,
        resolver_source,resolver_conf
    )
    d1,d2=st.columns(2)
    with d1:
        st.download_button(
            "Télécharger le rapport PDF",
            data=report_pdf,
            file_name="rapport_prm_v10a.pdf",
            mime="application/pdf",
            use_container_width=True
        )
    with d2:
        st.download_button(
            "Télécharger les données techniques JSON",
            data=json.dumps(snapshot,ensure_ascii=False,indent=2).encode("utf-8"),
            file_name="prm_snapshot_v10a.json",
            mime="application/json",
            use_container_width=True
        )

    with st.expander("Voir les données techniques PRM"):st.json(snapshot)
    st.caption(
    "Outil d’aide à la décision. Les éléments déterminants doivent être confirmés auprès des administrations, "
    "documents opposables ou professionnels compétents avant engagement."
)

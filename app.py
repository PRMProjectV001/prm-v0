import json, re
from datetime import datetime, timezone
import pandas as pd
import requests
import streamlit as st
from shapely.geometry import Point, shape, mapping
from shapely.ops import unary_union

st.set_page_config(page_title="PRM - Property Risk Management", page_icon="🏠", layout="wide")

GEOCODE_SEARCH="https://data.geopf.fr/geocodage/search"
GEOCODE_REVERSE="https://data.geopf.fr/geocodage/reverse"
CADASTRE_URL="https://apicarto.ign.fr/api/cadastre/parcelle"
GPU_BASE="https://apicarto.ign.fr/api/gpu"
GEORISQUES_REPORT="https://georisques.gouv.fr/api/v1/resultats_rapport_risque"
BDNB_BASE="https://api.bdnb.io/v1/bdnb"
TIMEOUT=20

def api_get(url,params=None):
    try:
        r=requests.get(url,params=params,timeout=TIMEOUT,headers={"User-Agent":"PRM-V0.3.1"})
        r.raise_for_status()
        return r.json(),None
    except Exception as e:
        return None,str(e)

def now_fr():
    return datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M")

def geocode(address):
    data,err=api_get(GEOCODE_SEARCH,{"q":address,"index":"address","limit":5,"autocomplete":"false"})
    if err or not data or not data.get("features"):
        return None,err or "Adresse introuvable."
    f=data["features"][0]; lon,lat=f["geometry"]["coordinates"]; p=f.get("properties",{})
    return {"label":p.get("label") or address,"lon":lon,"lat":lat,"citycode":p.get("citycode"),"postcode":p.get("postcode"),"city":p.get("city"),"score":p.get("score")},None

def reverse_parcel(lon,lat):
    data,err=api_get(GEOCODE_REVERSE,{"lon":lon,"lat":lat,"index":"parcel","limit":3})
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

def _find_values(obj, key_substr):
    vals=[]
    def walk(x):
        if isinstance(x,dict):
            for k,v in x.items():
                if key_substr.lower() in str(k).lower(): vals.append(v)
                walk(v)
        elif isinstance(x,list):
            for v in x: walk(v)
    walk(obj)
    return vals

def bdnb_parcel_ids(address):
    meta={}
    g,err=api_get(f"{BDNB_BASE}/geocodage",{"q":address})
    if err or not g:
        meta["geocodage_error"]=err
        return set(),meta
    keys=[v for v in _find_values(g,"cle_interop_adr") if isinstance(v,str) and v.strip()]
    if not keys:
        meta["geocodage_error"]="cle_interop_adr non trouvée"
        return set(),meta
    key=keys[0].strip(); meta["cle_interop_adr"]=key
    endpoint=f"{BDNB_BASE}/donnees/batiment_groupe_complet/adresse"
    responses=[]
    for params in ({"cle_interop_adr":f"eq.{key}","limit":20},{"cle_interop_adr":key,"limit":20}):
        data,e=api_get(endpoint,params)
        if not e and data:
            responses.append(data); break
        meta.setdefault("query_errors",[]).append(e)
    ids=set()
    def scan(x,parent=""):
        if isinstance(x,dict):
            for k,v in x.items(): scan(v,str(k))
        elif isinstance(x,list):
            for v in x: scan(v,parent)
        elif isinstance(x,(str,int,float)) and ("parcelle" in parent.lower() or "cadastr" in parent.lower()):
            s=str(x).upper()
            for m in re.finditer(r"([A-Z]{1,2})(\d{4})\b",s): ids.add(f"{m.group(1)} {m.group(2)}")
    for r in responses: scan(r)
    return ids,meta

def auto_resolve(address,lon,lat):
    candidates,cerr=get_candidates(lon,lat)
    reasons={}
    bdnb_ids,bdnb_meta=bdnb_parcel_ids(address)
    bdnb_matches=[f for d,f in candidates if parcel_id(f) in bdnb_ids]
    if bdnb_matches:
        for f in bdnb_matches: reasons[parcel_id(f)]="Liée au bâtiment/adresse dans la BDNB"
        return bdnb_matches[:3],reasons,"élevée","BDNB",{"cadastre":cerr,"bdnb":bdnb_meta},candidates

    rev,rerr=reverse_parcel(lon,lat); rid=reverse_ids(rev)
    matched=[(d,f) for d,f in candidates if parcel_id(f) in rid]
    matched.sort(key=lambda x:x[0])
    if matched:
        min_d=matched[0][0]; threshold=min_d+1.35
        strict=[f for d,f in matched if d<=threshold][:3]
        for f in strict: reasons[parcel_id(f)]="Géocodage inverse IGN + filtre strict de proximité"
        return strict,reasons,"moyenne","IGN reverse strict",{"cadastre":cerr,"reverse":rerr,"bdnb":bdnb_meta},candidates

    if candidates:
        d,f=candidates[0]; reasons[parcel_id(f)]=f"Parcelle la plus proche (~{d:.1f} m), à confirmer"
        return [f],reasons,"faible","fallback proximité",{"cadastre":cerr,"reverse":rerr,"bdnb":bdnb_meta},candidates
    return [],reasons,"faible","échec",{"cadastre":cerr,"reverse":rerr,"bdnb":bdnb_meta},candidates

def union_geom(features):
    return unary_union([shape(f["geometry"]) for f in features]).simplify(0.000001,preserve_topology=True)

def geojson_str(g): return json.dumps(mapping(g),separators=(",",":"))

def gpu_layer(layer,g):
    data,err=api_get(f"{GPU_BASE}/{layer}",{"geom":geojson_str(g),"_limit":200})
    return ((data or {}).get("features",[]) if data else []),err

def get_urbanism(g):
    layers={"zones":"zone-urba","prescriptions_surface":"prescription-surf","prescriptions_line":"prescription-lin","prescriptions_point":"prescription-pct","infos_surface":"info-surf"}
    out={}; errs={}
    for k,l in layers.items():
        out[k],e=gpu_layer(l,g)
        if e: errs[k]=e
    return out,errs

def zone_labels(features):
    labels=[]
    for f in features:
        p=f.get("properties",{})
        for k in ["libelle","libelle_zone","typezone","type_zone","nom","name"]:
            v=p.get(k)
            if isinstance(v,str) and v.strip(): labels.append(v.strip())
    labels=list(dict.fromkeys(labels)); specific=[x for x in labels if len(x)>1]
    return specific or labels

def get_report(lon,lat): return api_get(GEORISQUES_REPORT,{"latlon":f"{lon},{lat}"})
def norm(x): return " ".join(str(x or "").lower().split())

def subtrees(obj,aliases):
    aliases=[a.lower() for a in aliases]; hits=[]
    def walk(x,path=""):
        if isinstance(x,dict):
            nameish=" ".join(norm(x.get(k)) for k in x if str(k).lower() in ["libelle","nom","name","type","risque","libelle_risque","code_risque","titre","title"])
            context=f"{path} {' '.join(str(k).lower() for k in x)} {nameish}"
            if any(a in context for a in aliases): hits.append(x)
            for k,v in x.items(): walk(v,f"{path}/{str(k).lower()}")
        elif isinstance(x,list):
            for i,v in enumerate(x): walk(v,f"{path}/{i}")
    walk(obj); uniq=[]; seen=set()
    for h in hits:
        s=json.dumps(h,sort_keys=True,ensure_ascii=False,default=str)
        if s not in seen: seen.add(s); uniq.append(h)
    return uniq

def subtree_text(ss):
    vals=[]
    def walk(x):
        if isinstance(x,dict):
            for k,v in x.items(): vals.append(str(k)); walk(v)
        elif isinstance(x,list):
            for v in x: walk(v)
        elif isinstance(x,(str,int,float,bool)): vals.append(str(x))
    for s in ss: walk(s)
    return norm(" | ".join(vals))

def scale(text,den):
    m=re.search(rf"\b([1-{den}])\s*/\s*{den}\b",text)
    if m:return int(m.group(1))
    for pat in [r"\bniveau\s*[:=]?\s*([1-5])\b",r"\bzone\s*[:=]?\s*([1-5])\b",r"\bcatégorie\s*[:=]?\s*([1-5])\b",r"\bcategorie\s*[:=]?\s*([1-5])\b"]:
        m=re.search(pat,text)
        if m:
            n=int(m.group(1))
            if 1<=n<=den:return n
    return None

def distance_m(text):
    vals=[]
    for pat,mult in [(r"(\d+(?:[.,]\d+)?)\s*(?:mètres|metres|m)\b",1),(r"(\d+(?:[.,]\d+)?)\s*(?:km|kilomètres|kilometres)\b",1000)]:
        for m in re.finditer(pat,text):
            try: vals.append(float(m.group(1).replace(",","."))*mult)
            except: pass
    return round(min(vals),0) if vals else None

def item(status,label,source="Géorisques",scope="adresse",confidence="moyenne",official_level=None,details=None):
    return {"status":status,"label":label,"source":source,"scope":scope,"confidence":confidence,"official_level":official_level,"details":details or [],"checked_at":now_fr()}

def classify_radon(report):
    t=subtree_text(subtrees(report,["radon"]))
    if not t:return item("⚪","Niveau non récupéré",confidence="faible")
    n=scale(t,3)
    if n==1 or "potentiel radon faible" in t:return item("🟢","Potentiel radon faible",official_level="1/3")
    if n==2:return item("🟠","Potentiel radon intermédiaire",official_level="2/3")
    if n==3 or "potentiel radon élevé" in t or "potentiel radon eleve" in t:return item("🔴","Potentiel radon élevé",official_level="3/3")
    return item("⚪","Radon mentionné, niveau non interprétable",confidence="faible")

def classify_seisme(report):
    t=subtree_text(subtrees(report,["séisme","seisme","sismique"]))
    if not t:return item("⚪","Niveau non récupéré",confidence="faible")
    n=scale(t,5)
    if n in [1,2]:return item("🟢","Sismicité très faible" if n==1 else "Sismicité faible",official_level=f"{n}/5")
    if n==3:return item("🟠","Sismicité modérée",official_level="3/5")
    if n in [4,5]:return item("🔴","Sismicité forte",official_level=f"{n}/5")
    return item("⚪","Séisme mentionné, niveau non interprétable",confidence="faible")

def classify_argile(report):
    t=subtree_text(subtrees(report,["argile","retrait gonflement","gonflement"]))
    if not t:return item("⚪","Niveau non récupéré",confidence="faible")
    n=scale(t,3)
    if n==1 or "faible" in t:return item("🟢","Exposition argiles faible",official_level="Faible")
    if n==2 or "moyen" in t or "modéré" in t or "modere" in t:return item("🟠","Exposition argiles moyenne",official_level="Moyenne")
    if n==3 or "forte" in t or "fort" in t:return item("🔴","Exposition argiles forte",official_level="Forte")
    return item("⚪","Argiles mentionnées, niveau non interprétable",confidence="faible")

def classify_inondation(report):
    t=subtree_text(subtrees(report,["inondation","crue","submersion","remontée de nappe","remontee de nappe"]))
    if not t:return item("⚪","Aucune donnée exploitable récupérée",confidence="faible")
    if any(x in t for x in ["non concerné","non concerne","hors zone","faible"]):return item("🟢","Faible exposition détectée",official_level="Faible / non concerné")
    if any(x in t for x in ["zone rouge","aléa fort","alea fort","affecte votre bien"]):return item("🔴","Exposition significative détectée",official_level="Forte")
    if any(x in t for x in ["zone bleue","aléa moyen","alea moyen","territoire à risque important","territoire a risque important"]):return item("🟠","Élément d’inondation à vérifier",official_level="Intermédiaire")
    return item("⚪","Données d’inondation présentes, niveau à préciser",confidence="faible")

def classify_mouvements(report):
    t=subtree_text(subtrees(report,["mouvement de terrain","cavité","cavite","effondrement","glissement"]))
    if not t:return item("⚪","Aucun événement précis récupéré",confidence="faible")
    d=distance_m(t); details=[f"Objet le plus proche mentionné : ~{int(d)} m"] if d is not None else []
    if d is not None and d<=100:return item("🟠","Événement ou cavité proche à examiner",details=details)
    return item("⚪","Historique / données présents, exposition parcellaire non prouvée",confidence="faible",details=details)

def classify_tech(report):
    groups=[("SEVESO",["seveso"]),("ICPE / installation classée",["installation classée","installation classee","icpe"]),("Canalisation de transport",["canalisation"]),("Risque nucléaire",["nucléaire","nucleaire"])]
    detected=[]; dists=[]
    for label,aliases in groups:
        t=subtree_text(subtrees(report,aliases))
        if t:
            detected.append(label); d=distance_m(t)
            if d is not None:dists.append((label,d))
    if not detected:return item("🟢","Aucun signal technologique majeur extrait",official_level="Aucun signal extrait")
    details=[f"Type détecté : {x}" for x in detected]+[f"{lab} : distance mentionnée ~{int(d)} m" for lab,d in dists]
    nearest=min([d for _,d in dists],default=None)
    if nearest is not None and nearest<=250:return item("🟠","Risque technologique proche à vérifier",details=details)
    if nearest is not None:return item("🟢","Objets technologiques détectés mais non immédiats",details=details,official_level=f"Plus proche ~{int(nearest)} m")
    return item("⚪","Objet technologique mentionné, distance non interprétable",confidence="faible",details=details)

def row(f,candidates):
    p=f.get("properties",{}); dist=None
    for d,cf in candidates:
        if parcel_id(cf)==parcel_id(f):dist=round(d,1);break
    return {"Parcelle":parcel_id(f),"Surface m²":p.get("contenance"),"Distance approx.":dist,"ID cadastral":p.get("idu") or p.get("id")}

def card(name,x):
    lvl=f" · Niveau : {x['official_level']}" if x.get("official_level") else ""
    details="".join(f"<div style='margin-top:4px;color:#4b5563'>• {d}</div>" for d in x.get("details",[]))
    st.markdown(f"""<div style="border:1px solid #e5e7eb;border-radius:14px;padding:15px 17px;margin:7px 0;background:#fff">
    <div style="font-size:1.05rem;font-weight:750">{x['status']} {name}</div><div style="color:#374151;margin-top:5px">{x['label']}</div>{details}
    <div style="font-size:.77rem;color:#9ca3af;margin-top:9px">Source : {x['source']}{lvl}<br>Portée : {x['scope']} · Confiance : {x['confidence']}<br>Vérifié : {x['checked_at']}</div></div>""",unsafe_allow_html=True)

st.markdown('<div style="font-size:.8rem;letter-spacing:.12em;text-transform:uppercase;color:#9ca3af">Property Risk Management · Prototype V0.3.2</div>',unsafe_allow_html=True)
st.title("Avant d’acheter, voyez les risques.")
st.write("PRM identifie automatiquement le périmètre probable puis explique et source chaque feu.")

with st.form("search"):
    address=st.text_input("Adresse",value="27 rue des Jardins 92380 Garches")
    go=st.form_submit_button("Analyser cette propriété",use_container_width=True)

if go:
    with st.spinner("Identification de la propriété et analyse…"):
        geo,gerr=geocode(address)
        if not geo:st.error(gerr);st.stop()
        selected,reasons,resolver_conf,resolver_source,resolver_errors,candidates=auto_resolve(address,geo["lon"],geo["lat"])
        if not selected:st.error("PRM n’a pas pu identifier de parcelle probable.");st.stop()
        geom=union_geom(selected)
        urbanism,uerrors=get_urbanism(geom)
        report,rerr=get_report(geo["lon"],geo["lat"])

    rows=[row(f,candidates) for f in selected]; zones=zone_labels(urbanism.get("zones",[]))
    n_presc=sum(len(urbanism.get(k,[])) for k in ["prescriptions_surface","prescriptions_line","prescriptions_point","infos_surface"])
    cats={
        "Inondation / nappe":classify_inondation(report),
        "Argiles / sols":classify_argile(report),
        "Mouvements / cavités":classify_mouvements(report),
        "Séisme":classify_seisme(report),
        "Radon":classify_radon(report),
        "Risques technologiques":classify_tech(report),
        "Urbanisme":item("🟠" if zones else "⚪","Zonage détecté : "+", ".join(zones[:3]) if zones else "Zonage non récupéré",source="GPU / IGN",scope="parcelles",confidence="élevée" if zones else "faible",official_level=", ".join(zones[:3]) if zones else None,details=[f"{n_presc} prescription(s)/information(s) GPU intersectante(s)"]),
        "Climat 2050":item("⚪","Module officiel à connecter",source="À connecter",scope="commune",confidence="aucune"),
        "Végétation / vulnérabilité feu":item("⚪","Photos requises",source="Photos utilisateur",scope="propriété",confidence="aucune")
    }
    reds=sum(x["status"]=="🔴" for x in cats.values()); oranges=sum(x["status"]=="🟠" for x in cats.values()); greys=sum(x["status"]=="⚪" for x in cats.values())
    if reds:gs,gl="🔴","VIGILANCE FORTE"
    elif oranges>=2:gs,gl="🟠","VIGILANCE"
    elif oranges==1:gs,gl="🟠","VIGILANCE LIMITÉE"
    else:gs,gl="⚪","DONNÉES À COMPLÉTER"

    st.markdown(f"""<div style="padding:24px;border-radius:20px;background:#111827;color:white;margin-bottom:18px">
    <div style="font-size:.85rem;color:#9ca3af">PRM SNAPSHOT V0.3.2</div><div style="font-size:1.55rem;font-weight:800;margin-top:5px">{geo['label']}</div>
    <div style="font-size:1rem;color:#d1d5db;margin-top:4px">Parcelle(s) retenue(s) : {", ".join(x["Parcelle"] for x in rows)}</div>
    <div style="font-size:.9rem;color:#9ca3af;margin-top:3px">Resolver : {resolver_source} · Confiance : {resolver_conf}</div>
    <div style="font-size:2.1rem;font-weight:800;margin-top:14px">{gs} {gl}</div><div style="color:#d1d5db;margin-top:5px">{oranges} vigilances · {reds} fortes vigilances · {greys} points non évalués</div></div>""",unsafe_allow_html=True)

    c1,c2=st.columns(2)
    with c1:
        st.subheader("Localisation"); st.map(pd.DataFrame([{"lat":geo["lat"],"lon":geo["lon"]}]),zoom=17)
    with c2:
        st.subheader("Propriété identifiée"); st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)
        for rr in rows:st.caption(f"{rr['Parcelle']} — {reasons.get(rr['Parcelle'],'Sélection automatique PRM')}")
        st.caption("Résolution indicative si aucun lien explicite adresse-parcelle n’est disponible.")

    st.subheader("Feux PRM — avec preuves")
    cols=st.columns(2)
    for i,(name,x) in enumerate(cats.items()):
        with cols[i%2]:card(name,x)

    st.subheader("Urbanisme parcellaire"); st.write("**Zonage GPU :**"," · ".join(zones[:8]) if zones else "Non récupéré")
    st.write("Aucune prescription/information GPU particulière détectée sur le périmètre analysé." if n_presc==0 else f"{n_presc} prescription(s) ou information(s) GPU intersectent le périmètre.")

    st.subheader("Traçabilité PRM"); st.write("Chaque feu affiche sa source, son niveau lorsqu’il est identifiable, sa portée, la confiance et la date de vérification.")
    snapshot={"generated_at":datetime.now(timezone.utc).isoformat(),"address":geo,"property_resolver":{"source":resolver_source,"confidence":resolver_conf,"parcels":rows,"reasons":reasons,"errors":resolver_errors},"risks":cats,"urbanism":{"zones":zones,"prescription_count":n_presc},"errors":{"georisques":rerr,"gpu":uerrors}}
    with st.expander("Voir les données techniques PRM"):st.json(snapshot)
    st.download_button("Télécharger le snapshot JSON V0.3.1",data=json.dumps(snapshot,ensure_ascii=False,indent=2).encode("utf-8"),file_name="prm_snapshot_v032.json",mime="application/json",use_container_width=True)
    st.caption("Prototype d’aide à la décision. Ne remplace pas les diagnostics, études, états réglementaires ou conseils professionnels applicables.")

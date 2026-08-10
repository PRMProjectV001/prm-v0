import json, re, math
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
        r=requests.get(url,params=params,timeout=TIMEOUT,headers={"User-Agent":"PRM-V0.4"})
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
    rev,rerr=reverse_parcel(lon,lat); rids=reverse_ids(rev); strict=strict_reverse_selection(candidates,rids)
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

def get_report(lon,lat):return api_get(GEORISQUES_REPORT,{"latlon":f"{lon},{lat}"})
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
    t=subtree_text(subtrees(report,["radon"]))
    n=scale(t,3) if t else None
    if n==1 or "potentiel radon faible" in t:return item("🟢","Potentiel radon faible",official_level="1/3")
    if n==2:return item("🟠","Potentiel radon intermédiaire",official_level="2/3")
    if n==3:return item("🔴","Potentiel radon élevé",official_level="3/3")
    return item("⚪","Radon mentionné, niveau non interprétable",confidence="faible")

def classify_seisme(report):
    t=subtree_text(subtrees(report,["séisme","seisme","sismique"]))
    n=scale(t,5) if t else None
    if n in [1,2]:return item("🟢","Sismicité faible",official_level=f"{n}/5")
    if n==3:return item("🟠","Sismicité modérée",official_level="3/5")
    if n in [4,5]:return item("🔴","Sismicité forte",official_level=f"{n}/5")
    return item("⚪","Séisme mentionné, niveau non interprétable",confidence="faible")

def classify_argile(report):
    t=subtree_text(subtrees(report,["argile","retrait gonflement","gonflement"]))
    n=scale(t,3) if t else None
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
    groups=[("SEVESO",["seveso"]),("ICPE / installation classée",["installation classée","installation classee","icpe"]),("Canalisation de transport",["canalisation"]),("Risque nucléaire",["nucléaire","nucleaire"])]
    detected=[]
    for label,aliases in groups:
        if subtree_text(subtrees(report,aliases)):detected.append(label)
    if not detected:return item("🟢","Aucun signal technologique majeur extrait")
    return item("⚪","Objet technologique mentionné, distance non interprétable",confidence="faible",details=[f"Type détecté : {x}" for x in detected])

# ---------- Sitadel / projects ----------
def haversine(lat1,lon1,lat2,lon2):
    R=6371000
    p1,p2=math.radians(lat1),math.radians(lat2)
    dp=math.radians(lat2-lat1);dl=math.radians(lon2-lon1)
    a=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R*2*math.atan2(math.sqrt(a),math.sqrt(1-a))

def find_key_value(rec, includes, excludes=None):
    excludes=excludes or []
    for k,v in rec.items():
        lk=str(k).lower()
        if all(x in lk for x in includes) and not any(x in lk for x in excludes):
            if v not in [None,"",[]]:return v
    return None

def parse_float(v):
    try:return float(str(v).replace(",","."))
    except:return None

def extract_coords(rec):
    # explicit latitude/longitude fields first
    lat=lon=None
    for k,v in rec.items():
        lk=str(k).lower()
        if lk in ["latitude","lat"] or "latitude" in lk:lat=parse_float(v)
        if lk in ["longitude","lon","lng"] or "longitude" in lk:lon=parse_float(v)
    if lat is not None and lon is not None and -90<=lat<=90 and -180<=lon<=180:return lat,lon
    # Data Fair enriched geopoint
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

def project_address(rec):
    # direct address-like fields
    for k,v in rec.items():
        lk=str(k).lower()
        if ("adresse" in lk or "adr_" in lk) and isinstance(v,str) and len(v.strip())>4:
            return v.strip()
    num=find_key_value(rec,["num"],["log"])
    voie=find_key_value(rec,["voie"])
    commune=find_key_value(rec,["commune"]) or find_key_value(rec,["nom","comm"])
    parts=[str(x).strip() for x in [num,voie,commune] if x not in [None,""]]
    return " ".join(parts) if parts else None

def project_reference(rec):
    for token in ["num_dau","numero","num_aut","num_pc","num_pa","num_pd","id"]:
        for k,v in rec.items():
            if token in str(k).lower() and v not in [None,""]:
                s=str(v)
                if len(s)>4:return s
    return None

def project_description(rec,category):
    candidates=[]
    for k,v in rec.items():
        lk=str(k).lower()
        if any(x in lk for x in ["nature","type","destination","logement","travaux","projet"]):
            if isinstance(v,(str,int,float)) and str(v).strip() not in ["","0","None"]:
                txt=str(v).strip()
                if len(txt)<=120:candidates.append(txt)
    seen=[]
    for x in candidates:
        if x not in seen:seen.append(x)
    return " · ".join(seen[:3]) if seen else category

def query_sitadel_dataset(dataset_id,citycode,city):
    url=f"{DATAFAIR_BASE}/{dataset_id}/lines"
    attempts=[
        {"size":1000,"qs":f"COMM:{citycode}"},
        {"size":1000,"q":city or citycode},
    ]
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
        rows,err=query_sitadel_dataset(dataset,citycode,city)
        if err:errors[dataset]=err
        for rec in rows:
            plat,plon=extract_coords(rec)
            if plat is None or plon is None:continue
            dist=haversine(lat,lon,plat,plon)
            if dist>radius:continue
            dt,date_key=best_date(rec)
            # Keep all, but recent projects sort first. Mark age separately.
            age_label="Récent" if dt and dt.year>=2021 else ("Historique" if dt else "Date inconnue")
            out.append({
                "category":category,
                "dataset":dataset,
                "distance_m":round(dist),
                "date":dt.strftime("%d/%m/%Y") if dt else None,
                "date_sort":dt.timestamp() if dt else 0,
                "age_label":age_label,
                "address":project_address(rec),
                "reference":project_reference(rec),
                "description":project_description(rec,category),
                "lat":plat,"lon":plon,
            })
    # dedupe close duplicates / same reference
    dedup=[];seen=set()
    for p in sorted(out,key=lambda x:(-x["date_sort"],x["distance_m"])):
        key=p["reference"] or (p["category"],p["address"],p["date"],p["distance_m"]//10)
        if key in seen:continue
        seen.add(key);dedup.append(p)
    return dedup,errors

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

st.markdown('<div style="font-size:.8rem;letter-spacing:.12em;text-transform:uppercase;color:#9ca3af">Property Risk Management · Prototype V0.4</div>',unsafe_allow_html=True)
st.title("Avant d’acheter, voyez les risques.")
st.write("V0.4 ajoute les autorisations Sitadel géolocalisées autour de la propriété.")

with st.form("search"):
    address=st.text_input("Adresse",value="27 rue des Jardins 92380 Garches")
    radius=st.select_slider("Rayon projets voisins",options=[100,250,500],value=500,format_func=lambda x:f"{x} m")
    go=st.form_submit_button("Analyser cette propriété",use_container_width=True)

if go:
    with st.spinner("Identification, risques, urbanisme et projets voisins…"):
        geo,gerr=geocode(address)
        if not geo:st.error(gerr);st.stop()
        selected,reasons,resolver_conf,resolver_source,resolver_errors,candidates=auto_resolve(address,geo["lon"],geo["lat"])
        if not selected:st.error("PRM n’a pas pu identifier de parcelle probable.");st.stop()
        geom=union_geom(selected)
        urbanism,uerrors=get_urbanism(geom)
        report,rerr=get_report(geo["lon"],geo["lat"])
        projects,project_errors=get_projects(geo["lat"],geo["lon"],geo.get("citycode"),geo.get("city"),radius)

    rows=[row(f,candidates) for f in selected];zones=zone_labels(urbanism.get("zones",[]))
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
    reds=sum(x["status"]=="🔴" for x in cats.values());oranges=sum(x["status"]=="🟠" for x in cats.values());greys=sum(x["status"]=="⚪" for x in cats.values())
    if reds:gs,gl="🔴","VIGILANCE FORTE"
    elif oranges>=2:gs,gl="🟠","VIGILANCE"
    elif oranges==1:gs,gl="🟠","VIGILANCE LIMITÉE"
    else:gs,gl="⚪","DONNÉES À COMPLÉTER"

    st.markdown(f"""<div style="padding:24px;border-radius:20px;background:#111827;color:white;margin-bottom:18px">
    <div style="font-size:.85rem;color:#9ca3af">PRM SNAPSHOT V0.4</div><div style="font-size:1.55rem;font-weight:800;margin-top:5px">{geo['label']}</div>
    <div style="font-size:1rem;color:#d1d5db;margin-top:4px">Parcelle(s) : {", ".join(x["Parcelle"] for x in rows)}</div>
    <div style="font-size:.9rem;color:#9ca3af;margin-top:3px">Resolver : {resolver_source} · Confiance : {resolver_conf}</div>
    <div style="font-size:2.1rem;font-weight:800;margin-top:14px">{gs} {gl}</div></div>""",unsafe_allow_html=True)

    c1,c2=st.columns(2)
    with c1:
        st.subheader("Localisation");st.map(pd.DataFrame([{"lat":geo["lat"],"lon":geo["lon"]}]),zoom=17)
    with c2:
        st.subheader("Propriété identifiée");st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)

    st.subheader("Feux PRM — avec preuves")
    cols=st.columns(2)
    for i,(name,x) in enumerate(cats.items()):
        with cols[i%2]:card(name,x)

    st.subheader("🏗 Projets & autorisations autour du bien")
    st.caption(f"Recherche Sitadel dans un rayon de {radius} m. Les données ouvertes sont diffusées mensuellement ; un dossier absent ne signifie pas qu’aucun projet n’existe.")
    if not projects:
        st.info("Aucune autorisation géolocalisée détectée dans ce rayon via les jeux Sitadel interrogés.")
    else:
        recent=[p for p in projects if p["age_label"]=="Récent"]
        st.write(f"**{len(projects)} autorisation(s) détectée(s)**, dont **{len(recent)} récente(s) (2021+)**.")
        for p in projects[:15]:
            icon="🟠" if p["age_label"]=="Récent" and p["distance_m"]<=250 else "⚪"
            title=f"{icon} {p['category']} — {p['distance_m']} m"
            with st.expander(title,expanded=(p is projects[0])):
                st.write("**Date :**",p["date"] or "Non interprétée")
                st.write("**Adresse / localisation :**",p["address"] or "Parcelle géolocalisée, adresse non extraite")
                st.write("**Description :**",p["description"])
                if p["reference"]:st.write("**Référence :**",p["reference"])
                st.write("**Lecture PRM :**","Projet récent à examiner pour vue, luminosité, nuisances ou circulation." if icon=="🟠" else "Autorisation historique / éloignée : contexte du quartier.")
                st.caption(f"Source : Sitadel / SDES via jeu géolocalisé Koumoul ({p['dataset']})")

        map_rows=[{"lat":geo["lat"],"lon":geo["lon"],"type":"Bien analysé"}]+[
            {"lat":p["lat"],"lon":p["lon"],"type":p["category"]} for p in projects[:50]
        ]
        st.map(pd.DataFrame(map_rows),zoom=15)

    st.subheader("Urbanisme parcellaire")
    st.write("**Zonage GPU :**"," · ".join(zones[:8]) if zones else "Non récupéré")
    st.write("Aucune prescription/information GPU particulière détectée sur le périmètre analysé." if n_presc==0 else f"{n_presc} prescription(s) ou information(s) GPU intersectent le périmètre.")

    snapshot={"generated_at":datetime.now(timezone.utc).isoformat(),"address":geo,"property_resolver":{"source":resolver_source,"confidence":resolver_conf,"parcels":rows},"risks":cats,"projects":{"radius_m":radius,"results":projects,"errors":project_errors},"urbanism":{"zones":zones,"prescription_count":n_presc}}
    with st.expander("Voir les données techniques PRM"):st.json(snapshot)
    st.download_button("Télécharger le snapshot JSON V0.4",data=json.dumps(snapshot,ensure_ascii=False,indent=2).encode("utf-8"),file_name="prm_snapshot_v04.json",mime="application/json",use_container_width=True)
    st.caption("Prototype d’aide à la décision. Les données Sitadel peuvent être incomplètes ou retardées ; vérifier les dossiers importants auprès du service urbanisme compétent.")

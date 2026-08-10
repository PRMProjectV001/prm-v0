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
        r=requests.get(url,params=params,timeout=TIMEOUT,headers={"User-Agent":"PRM-V0.5"})
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
    t=subtree_text(subtrees(report,["radon"])); n=scale(t,3) if t else None
    if n==1 or "potentiel radon faible" in t:return item("🟢","Potentiel radon faible",official_level="1/3")
    if n==2:return item("🟠","Potentiel radon intermédiaire",official_level="2/3")
    if n==3:return item("🔴","Potentiel radon élevé",official_level="3/3")
    return item("⚪","Radon mentionné, niveau non interprétable",confidence="faible")

def classify_seisme(report):
    t=subtree_text(subtrees(report,["séisme","seisme","sismique"])); n=scale(t,5) if t else None
    if n in [1,2]:return item("🟢","Sismicité faible",official_level=f"{n}/5")
    if n==3:return item("🟠","Sismicité modérée",official_level="3/5")
    if n in [4,5]:return item("🔴","Sismicité forte",official_level=f"{n}/5")
    return item("⚪","Séisme mentionné, niveau non interprétable",confidence="faible")

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
    groups=[("SEVESO",["seveso"]),("ICPE / installation classée",["installation classée","installation classee","icpe"]),("Canalisation de transport",["canalisation"]),("Risque nucléaire",["nucléaire","nucleaire"])]
    detected=[]
    for label,aliases in groups:
        if subtree_text(subtrees(report,aliases)):detected.append(label)
    if not detected:return item("🟢","Aucun signal technologique majeur extrait")
    return item("⚪","Objet technologique mentionné, distance non interprétable",confidence="faible",details=[f"Type détecté : {x}" for x in detected])


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

st.markdown('<div style="font-size:.8rem;letter-spacing:.12em;text-transform:uppercase;color:#9ca3af">Property Risk Management · Prototype V0.5</div>',unsafe_allow_html=True)
st.title("Avant d’acheter, voyez les risques.")
st.write("V0.5 ajoute un module Climat 2050 fondé sur la TRACC et les projections Météo-France.")

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
        climate_profile=climate_2050_profile(geo)

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
        "Climat 2050":item("🟠" if climate_profile.get("available") else "⚪","Adaptation à anticiper" if climate_profile.get("available") else "Données locales à compléter",source="Météo-France / TRACC",scope="région / département",confidence="élevée" if climate_profile.get("available") else "moyenne"),
        "Végétation / vulnérabilité feu":item("⚪","Photos requises",source="Photos utilisateur",scope="propriété",confidence="aucune")
    }
    reds=sum(x["status"]=="🔴" for x in cats.values());oranges=sum(x["status"]=="🟠" for x in cats.values())
    if reds:gs,gl="🔴","VIGILANCE FORTE"
    elif oranges>=2:gs,gl="🟠","VIGILANCE"
    elif oranges==1:gs,gl="🟠","VIGILANCE LIMITÉE"
    else:gs,gl="⚪","DONNÉES À COMPLÉTER"

    st.markdown(f"""<div style="padding:24px;border-radius:20px;background:#111827;color:white;margin-bottom:18px">
    <div style="font-size:.85rem;color:#9ca3af">PRM SNAPSHOT V0.5</div><div style="font-size:1.55rem;font-weight:800;margin-top:5px">{geo['label']}</div>
    <div style="font-size:1rem;color:#d1d5db;margin-top:4px">Parcelle(s) : {", ".join(x["Parcelle"] for x in rows)}</div>
    <div style="font-size:.9rem;color:#9ca3af;margin-top:3px">Resolver : {resolver_source} · Confiance : {resolver_conf}</div>
    <div style="font-size:2.1rem;font-weight:800;margin-top:14px">{gs} {gl}</div></div>""",unsafe_allow_html=True)

    c1,c2=st.columns(2)
    with c1:
        st.subheader("Localisation");st.map(pd.DataFrame([{"lat":geo["lat"],"lon":geo["lon"]}]),zoom=17,size=12)
    with c2:
        st.subheader("Propriété identifiée");st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)

    st.subheader("Feux PRM — avec preuves")
    cols=st.columns(2)
    for i,(name,x) in enumerate(cats.items()):
        with cols[i%2]:card(name,x)

    st.subheader("🏗 Projets & autorisations autour du bien")
    st.caption(f"Sitadel dans un rayon de {radius} m. Les autorisations sont diffusées mensuellement et peuvent comporter un délai de remontée.")

    priority_projects=[p for p in projects if p["priority"]<=1]
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

    snapshot={"generated_at":datetime.now(timezone.utc).isoformat(),"address":geo,"property_resolver":{"source":resolver_source,"confidence":resolver_conf,"parcels":rows},"risks":cats,"projects":{"radius_m":radius,"priority":priority_projects,"all":projects,"errors":project_errors},"climate_2050":climate_profile,"urbanism":{"zones":zones,"prescription_count":n_presc}}
    with st.expander("Voir les données techniques PRM"):st.json(snapshot)
    st.download_button("Télécharger le snapshot JSON V0.5",data=json.dumps(snapshot,ensure_ascii=False,indent=2).encode("utf-8"),file_name="prm_snapshot_v05.json",mime="application/json",use_container_width=True)
    st.caption("Prototype d’aide à la décision. Vérifier les dossiers importants auprès du service urbanisme compétent.")

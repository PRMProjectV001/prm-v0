import json
from datetime import datetime, timezone
import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="PRM - Property Risk Management", page_icon="🏠", layout="wide")

GEOCODE_URL = "https://data.geopf.fr/geocodage/search"
CADASTRE_URL = "https://apicarto.ign.fr/api/cadastre/parcelle"
GPU_BASE = "https://apicarto.ign.fr/api/gpu"
GEORISQUES_BASE = "https://georisques.gouv.fr/api/v1"
TIMEOUT = 18

def api_get(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent":"PRM-V0.2"})
        r.raise_for_status()
        return r.json(), None
    except Exception as e:
        return None, str(e)

def geocode(address):
    data, err = api_get(GEOCODE_URL, {"q":address,"index":"address","limit":5,"autocomplete":"false"})
    if err or not data or not data.get("features"):
        return None, err or "Adresse introuvable."
    f = data["features"][0]
    lon, lat = f["geometry"]["coordinates"]
    p = f.get("properties", {})
    return {"label":p.get("label") or address,"lon":lon,"lat":lat,"citycode":p.get("citycode"),"score":p.get("score")}, None

def geom_point(lon, lat):
    return json.dumps({"type":"Point","coordinates":[lon,lat]}, separators=(",",":"))

def get_parcels(lon, lat):
    data, err = api_get(CADASTRE_URL, {"geom":geom_point(lon,lat),"_limit":20})
    feats = (data or {}).get("features", []) if data else []
    if feats:
        return feats, None, "point"
    d = 0.00018
    poly = {"type":"Polygon","coordinates":[[[lon-d,lat-d],[lon+d,lat-d],[lon+d,lat+d],[lon-d,lat+d],[lon-d,lat-d]]]}
    data2, err2 = api_get(CADASTRE_URL, {"geom":json.dumps(poly,separators=(",",":")),"_limit":50})
    return ((data2 or {}).get("features", []) if data2 else []), err2 or err, "proximité"

def get_georisks(lon, lat, citycode=None):
    data, err = api_get(f"{GEORISQUES_BASE}/resultats_rapport_risque", {"latlon":f"{lon},{lat}"})
    if data and not err:
        return data, None, "adresse"
    if citycode:
        fb, ferr = api_get(f"{GEORISQUES_BASE}/gaspar/risques", {"code_insee":citycode})
        if fb and not ferr:
            return fb, None, "commune"
        return None, ferr or err, "indisponible"
    return None, err, "indisponible"

def gpu_layer(layer, geom):
    data, err = api_get(f"{GPU_BASE}/{layer}", {"geom":geom,"_limit":100})
    return ((data or {}).get("features", []) if data else []), err

def get_urbanism(lon, lat):
    geom = geom_point(lon, lat)
    layers = {"zones":"zone-urba","prescriptions_surface":"prescription-surf","prescriptions_line":"prescription-lin","prescriptions_point":"prescription-pct","infos_surface":"info-surf"}
    out, errs = {}, {}
    for k,l in layers.items():
        out[k], e = gpu_layer(l, geom)
        if e: errs[k]=e
    return out, errs

def scalar_text(obj):
    vals=[]
    def walk(x):
        if isinstance(x,dict):
            for v in x.values(): walk(v)
        elif isinstance(x,list):
            for v in x: walk(v)
        elif isinstance(x,(str,int,float,bool)):
            vals.append(str(x))
    walk(obj)
    return " | ".join(vals).lower()

def risk_status(risks, keywords):
    hay = scalar_text(risks) if risks else ""
    if not any(k in hay for k in keywords):
        return {"status":"⚪","label":"Non détecté / à préciser","confidence":"low"}
    snippets=[]
    for kw in keywords:
        pos = hay.find(kw)
        if pos >= 0:
            snippets.append(hay[max(0,pos-180):pos+260])
    local = " | ".join(snippets) or hay
    if any(w in local for w in ["très fort","tres fort","fort","important","élevé","eleve"]):
        return {"status":"🔴","label":"Vigilance forte","confidence":"medium"}
    if any(w in local for w in ["faible","très faible","tres faible","non concerné","non concerne"]):
        return {"status":"🟢","label":"Faible vigilance","confidence":"medium"}
    if any(w in local for w in ["modéré","modere","moyen","concerné","concerne","existant"]):
        return {"status":"🟠","label":"Vigilance","confidence":"medium"}
    return {"status":"⚪","label":"Risque mentionné, niveau à préciser","confidence":"low"}

def zone_labels(features):
    labels=[]
    for f in features:
        p=f.get("properties",{})
        for key in ["libelle","libelle_zone","typezone","type_zone","nom","name"]:
            v=p.get(key)
            if isinstance(v,str) and v.strip():
                labels.append(v.strip())
    labels=list(dict.fromkeys(labels))
    specific=[x for x in labels if len(x)>1]
    return specific or labels

def parcel_rows(features):
    rows=[]
    for f in features[:10]:
        p=f.get("properties",{})
        rows.append({"Commune":p.get("nom_com") or p.get("commune") or p.get("code_insee"),
                     "Section":p.get("section"),
                     "Parcelle":p.get("numero") or p.get("numero_parcelle"),
                     "Contenance m²":p.get("contenance"),
                     "ID":p.get("idu") or p.get("id")})
    return rows

def make_assessment(geo, parcels, parcel_scope, risks, risk_scope, urbanism):
    defs={"Inondation":["inond","crue","submersion"],
          "Argiles / sols":["argile","retrait","gonflement"],
          "Mouvements / cavités":["mouvement","cavité","cavite","effondrement","glissement"],
          "Séisme":["séism","seism"],
          "Radon":["radon"],
          "Incendie / forêt":["feu de forêt","feux de forêt","incendie","forestier"],
          "Risques technologiques":["seveso","industriel","canalisation","nucléaire","nucleaire"]}
    categories={}
    for name,kws in defs.items():
        item=risk_status(risks,kws)
        item["scope"]=risk_scope
        categories[name]=item
    zones=zone_labels(urbanism.get("zones",[]))
    categories["Urbanisme"]={"status":"🟠" if zones else "⚪","label":("Zonage détecté : "+", ".join(zones[:3])) if zones else "Zonage à préciser","confidence":"medium" if zones else "low","scope":"point"}
    categories["Climat 2050"]={"status":"⚪","label":"Module à connecter","confidence":"none","scope":"commune"}
    categories["Végétation / vulnérabilité feu"]={"status":"⚪","label":"Photos requises","confidence":"none","scope":"property"}
    reds=sum(v["status"]=="🔴" for v in categories.values())
    oranges=sum(v["status"]=="🟠" for v in categories.values())
    unknown=sum(v["status"]=="⚪" for v in categories.values())
    if reds: gs,gl="🔴","Vigilance forte"
    elif oranges>=2: gs,gl="🟠","Vigilance"
    elif oranges==1: gs,gl="🟠","Vigilance limitée"
    else: gs,gl="⚪","Données à compléter"
    return {"generated_at":datetime.now(timezone.utc).isoformat(),"address":geo,"parcels":parcel_rows(parcels),"parcel_scope":parcel_scope,"categories":categories,"summary":{"status":gs,"label":gl,"orange_count":oranges,"red_count":reds,"unknown_count":unknown}}

def card(name,item):
    st.markdown(f'''<div style="border:1px solid #e5e7eb;border-radius:14px;padding:14px 16px;margin:7px 0;background:white">
    <div style="font-size:1.05rem;font-weight:700">{item["status"]} {name}</div>
    <div style="color:#4b5563">{item["label"]}</div>
    <div style="font-size:.78rem;color:#9ca3af;margin-top:5px">Confiance: {item["confidence"]} · Portée: {item["scope"]}</div></div>''', unsafe_allow_html=True)

st.markdown('<div style="font-size:.8rem;letter-spacing:.12em;text-transform:uppercase;color:#9ca3af">Property Risk Management · Prototype V0.2</div>', unsafe_allow_html=True)
st.title("Avant d’acheter, voyez les risques.")
st.write("Entrez une adresse française. PRM construit un snapshot prudent à partir de sources publiques.")

with st.form("search"):
    address=st.text_input("Adresse", value="27 rue des Jardins 92380 Garches")
    submitted=st.form_submit_button("Analyser cette propriété", use_container_width=True)

if submitted:
    with st.spinner("Analyse des sources publiques…"):
        geo,gerr=geocode(address)
        if not geo:
            st.error(gerr); st.stop()
        parcels,perr,parcel_scope=get_parcels(geo["lon"],geo["lat"])
        risks,rerr,risk_scope=get_georisks(geo["lon"],geo["lat"],geo.get("citycode"))
        urbanism,uerrors=get_urbanism(geo["lon"],geo["lat"])
        a=make_assessment(geo,parcels,parcel_scope,risks,risk_scope,urbanism)

    s=a["summary"]
    st.markdown(f'''<div style="padding:24px;border-radius:20px;background:#111827;color:white;margin-bottom:18px">
    <div style="font-size:.85rem;color:#9ca3af">PRM SNAPSHOT</div>
    <div style="font-size:1.55rem;font-weight:800;margin-top:5px">{geo["label"]}</div>
    <div style="font-size:2.1rem;font-weight:800;margin-top:14px">{s["status"]} {s["label"].upper()}</div>
    <div style="color:#d1d5db;margin-top:5px">{s["orange_count"]} vigilances · {s["red_count"]} fortes vigilances · {s["unknown_count"]} points non évalués</div></div>''', unsafe_allow_html=True)

    c1,c2=st.columns(2)
    with c1:
        st.subheader("Localisation")
        st.map(pd.DataFrame([{"lat":geo["lat"],"lon":geo["lon"]}]), zoom=16)
    with c2:
        st.subheader("Parcelle")
        rows=a["parcels"]
        if rows:
            st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)
            if parcel_scope=="proximité":
                st.caption("Parcelles proches trouvées : confirmation exacte nécessaire.")
        else:
            st.info("Parcelle non identifiée automatiquement.")

    st.subheader("Feux PRM")
    cols=st.columns(2)
    for i,(name,item) in enumerate(a["categories"].items()):
        with cols[i%2]:
            card(name,item)

    st.subheader("Urbanisme détecté")
    zones=zone_labels(urbanism.get("zones",[]))
    st.write("Zonage GPU détecté :", " · ".join(zones[:5]) if zones else "À préciser")
    n_constraints=sum(len(v) for k,v in urbanism.items() if k!="zones")
    st.caption(f"{n_constraints} objet(s) de prescription/information GPU intersectent le point interrogé.")

    st.subheader("Ce que PRM refuse d’inventer")
    st.write("Pas de couleur Climat 2050 sans source dédiée. Pas de note de végétation sans photos. Pas de vert par défaut quand une donnée manque.")

    with st.expander("Voir le JSON PRM"):
        st.json(a)

    st.download_button("Télécharger le snapshot JSON",
        data=json.dumps(a,ensure_ascii=False,indent=2).encode("utf-8"),
        file_name="prm_snapshot_v02.json",
        mime="application/json",
        use_container_width=True)
else:
    st.info("Le moteur ne s’exécute qu’après clic sur « Analyser cette propriété ».")

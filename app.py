
import json
import math
from datetime import datetime, timezone
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="PRM - Property Risk Management",
    page_icon="🏠",
    layout="wide",
)

GEOCODE_URL = "https://data.geopf.fr/geocodage/search"
CADASTRE_URL = "https://apicarto.ign.fr/api/cadastre/parcelle"
GPU_BASE = "https://apicarto.ign.fr/api/gpu"
GEORISQUES_BASE = "https://georisques.gouv.fr/api/v1"

TIMEOUT = 18

def api_get(url, params=None):
    try:
        r = requests.get(
            url,
            params=params,
            timeout=TIMEOUT,
            headers={"User-Agent": "PRM-V0/0.1 (+prototype)"},
        )
        r.raise_for_status()
        return r.json(), None
    except Exception as e:
        return None, str(e)

def point_geojson(lon, lat):
    return json.dumps({"type": "Point", "coordinates": [lon, lat]}, separators=(",", ":"))

def geocode(address):
    data, err = api_get(
        GEOCODE_URL,
        {
            "q": address,
            "index": "address",
            "limit": 5,
            "autocomplete": "false",
        },
    )
    if err or not data or not data.get("features"):
        return None, err or "Adresse introuvable."
    f = data["features"][0]
    lon, lat = f["geometry"]["coordinates"]
    p = f.get("properties", {})
    return {
        "label": p.get("label") or address,
        "lon": lon,
        "lat": lat,
        "citycode": p.get("citycode"),
        "postcode": p.get("postcode"),
        "city": p.get("city"),
        "score": p.get("score"),
        "raw": f,
    }, None

def get_parcels(lon, lat):
    geom = point_geojson(lon, lat)
    data, err = api_get(CADASTRE_URL, {"geom": geom, "_limit": 20})
    if err or not data:
        return [], err
    return data.get("features", []), None

def get_georisks(lon, lat, citycode=None):
    # Primary "all-risk" endpoint. If it changes, commune inventory remains a fallback.
    data, err = api_get(
        f"{GEORISQUES_BASE}/resultats_rapport_risque",
        {"latlon": f"{lon},{lat}"},
    )
    if not err and data:
        return data, None, "address"

    if citycode:
        fallback, ferr = api_get(
            f"{GEORISQUES_BASE}/gaspar/risques",
            {"code_insee": citycode},
        )
        if not ferr and fallback:
            return fallback, None, "commune"
        return None, ferr or err, "unavailable"
    return None, err, "unavailable"

def gpu_layer(layer, geom):
    data, err = api_get(f"{GPU_BASE}/{layer}", {"geom": geom, "_limit": 100})
    if err or not data:
        return [], err
    return data.get("features", []), None

def get_urbanism(lon, lat):
    geom = point_geojson(lon, lat)
    layers = {
        "zones": "zone-urba",
        "prescriptions_surface": "prescription-surf",
        "prescriptions_line": "prescription-lin",
        "prescriptions_point": "prescription-pct",
        "infos_surface": "info-surf",
    }
    out = {}
    errors = {}
    for key, layer in layers.items():
        feats, err = gpu_layer(layer, geom)
        out[key] = feats
        if err:
            errors[key] = err
    return out, errors

def extract_risk_texts(obj):
    texts = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                lk = str(k).lower()
                if isinstance(v, (str, int, float)) and any(
                    token in lk for token in ["libelle", "risque", "alea", "niveau", "exposition", "zonage"]
                ):
                    texts.append(str(v))
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    # de-duplicate preserving order
    seen = set()
    out = []
    for t in texts:
        t2 = " ".join(t.split())
        if t2 and t2.lower() not in seen:
            seen.add(t2.lower())
            out.append(t2)
    return out[:80]

def classify_keyword(texts, keywords):
    hay = " | ".join(texts).lower()
    if not any(k in hay for k in keywords):
        return "⚪", "Non détecté / à préciser", "low"
    # Conservative V0: presence alone => vigilance, never red without structured address-level level.
    return "🟠", "Vigilance", "medium"

def extract_zone_labels(features):
    labels = []
    for f in features:
        p = f.get("properties", {})
        for key in ["libelle", "libelle_zone", "typezone", "type_zone", "nom", "name", "libellelong"]:
            if p.get(key):
                labels.append(str(p[key]))
    return list(dict.fromkeys(labels))

def parcel_summary(features):
    rows = []
    for f in features:
        p = f.get("properties", {})
        rows.append({
            "Commune": p.get("nom_com") or p.get("commune") or p.get("code_insee"),
            "Section": p.get("section"),
            "Parcelle": p.get("numero") or p.get("numero_parcelle"),
            "Contenance m²": p.get("contenance"),
            "ID": p.get("idu") or p.get("id"),
        })
    return rows

def make_assessment(geo, parcels, risks, risk_scope, urbanism):
    risk_texts = extract_risk_texts(risks) if risks else []

    categories = {}
    definitions = {
        "Inondation": ["inond", "crue", "submersion"],
        "Argiles / sols": ["argile", "retrait", "gonflement"],
        "Mouvements / cavités": ["mouvement", "cavité", "cavite", "effondrement", "glissement"],
        "Séisme": ["séism", "seism"],
        "Radon": ["radon"],
        "Incendie / forêt": ["feu de forêt", "feux de forêt", "incendie", "forestier"],
        "Risques technologiques": ["industriel", "seveso", "canalisation", "nucléaire", "nucleaire"],
    }
    for name, kws in definitions.items():
        icon, label, confidence = classify_keyword(risk_texts, kws)
        categories[name] = {
            "status": icon,
            "label": label,
            "confidence": confidence,
            "scope": risk_scope,
        }

    zones = extract_zone_labels(urbanism.get("zones", []))
    if zones:
        categories["Urbanisme"] = {
            "status": "🟠",
            "label": "Règles applicables détectées",
            "confidence": "medium",
            "scope": "point",
            "details": zones[:5],
        }
    else:
        categories["Urbanisme"] = {
            "status": "⚪",
            "label": "À préciser",
            "confidence": "low",
            "scope": "point",
        }

    # Climate and photo-vulnerability are intentionally not fabricated in V0.
    categories["Climat 2050"] = {
        "status": "⚪",
        "label": "Module à connecter",
        "confidence": "none",
        "scope": "commune",
    }
    categories["Végétation / vulnérabilité feu"] = {
        "status": "⚪",
        "label": "Photos requises",
        "confidence": "none",
        "scope": "property",
    }

    orange = sum(1 for v in categories.values() if v["status"] == "🟠")
    red = sum(1 for v in categories.values() if v["status"] == "🔴")
    unknown = sum(1 for v in categories.values() if v["status"] == "⚪")
    global_label = "Vigilance forte" if red else ("Vigilance" if orange >= 2 else "Faible vigilance")
    global_icon = "🔴" if red else ("🟠" if orange >= 2 else "🟢")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "address": geo,
        "parcels": parcel_summary(parcels),
        "categories": categories,
        "summary": {
            "status": global_icon,
            "label": global_label,
            "orange_count": orange,
            "red_count": red,
            "unknown_count": unknown,
        },
        "disclaimer": (
            "Prototype d'aide à la décision. Un feu indique un niveau de vigilance dans les "
            "sources interrogées, pas une garantie de sécurité ni un diagnostic réglementaire, "
            "juridique, géotechnique ou assurantiel."
        ),
    }

def render_category(name, item):
    st.markdown(
        f"""
        <div style="border:1px solid #e5e7eb;border-radius:14px;padding:14px 16px;margin:7px 0;background:white">
        <div style="font-size:1.05rem;font-weight:700">{item['status']} {name}</div>
        <div style="color:#4b5563">{item['label']}</div>
        <div style="font-size:.78rem;color:#9ca3af;margin-top:5px">Confiance: {item['confidence']} · Portée: {item['scope']}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown(
    """
    <style>
      .block-container {max-width: 1120px; padding-top: 2rem;}
      h1 {letter-spacing:-.03em;}
      .prm-hero {padding:24px;border-radius:20px;background:#111827;color:white;margin-bottom:18px}
      .prm-kicker {font-size:.8rem;letter-spacing:.12em;text-transform:uppercase;color:#9ca3af}
      .small-note {color:#6b7280;font-size:.85rem}
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="prm-kicker">Property Risk Management · Prototype V0</div>', unsafe_allow_html=True)
st.title("Avant d’acheter, voyez les risques.")
st.write("Entrez une adresse française. PRM interroge automatiquement des sources publiques et construit un premier snapshot.")

with st.form("search"):
    address = st.text_input("Adresse", value="27 rue des Jardins 92380 Garches")
    submitted = st.form_submit_button("Analyser cette propriété", use_container_width=True)

if submitted:
    with st.spinner("Analyse des sources publiques…"):
        geo, gerr = geocode(address)
        if not geo:
            st.error(f"Géocodage impossible : {gerr}")
            st.stop()

        parcels, perr = get_parcels(geo["lon"], geo["lat"])
        risks, rerr, risk_scope = get_georisks(geo["lon"], geo["lat"], geo.get("citycode"))
        urbanism, uerrors = get_urbanism(geo["lon"], geo["lat"])
        assessment = make_assessment(geo, parcels, risks, risk_scope, urbanism)

    s = assessment["summary"]
    st.markdown(
        f"""
        <div class="prm-hero">
          <div style="font-size:.85rem;color:#9ca3af">PRM SNAPSHOT</div>
          <div style="font-size:1.55rem;font-weight:800;margin-top:5px">{geo['label']}</div>
          <div style="font-size:2.1rem;font-weight:800;margin-top:14px">{s['status']} {s['label'].upper()}</div>
          <div style="color:#d1d5db;margin-top:5px">{s['orange_count']} vigilances · {s['red_count']} fortes vigilances · {s['unknown_count']} points non évalués</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns([1,1])
    with c1:
        st.subheader("Localisation")
        st.map(pd.DataFrame([{"lat": geo["lat"], "lon": geo["lon"]}]), zoom=16)
        st.caption(f"Code INSEE : {geo.get('citycode') or 'n/a'} · Géocodage : {geo.get('score') or 'n/a'}")
    with c2:
        st.subheader("Parcelle")
        rows = assessment["parcels"]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            st.info("Aucune parcelle renvoyée automatiquement pour ce point.")
        if perr:
            st.caption("API Cadastre : " + perr)

    st.subheader("Feux PRM")
    cols = st.columns(2)
    for i, (name, item) in enumerate(assessment["categories"].items()):
        with cols[i % 2]:
            render_category(name, item)

    st.subheader("Urbanisme détecté")
    zone_labels = extract_zone_labels(urbanism.get("zones", []))
    if zone_labels:
        for z in zone_labels[:10]:
            st.write("•", z)
    else:
        st.info("Aucun libellé de zone exploitable n’a été extrait automatiquement.")
    n_constraints = sum(len(v) for k, v in urbanism.items() if k != "zones")
    st.caption(f"{n_constraints} objet(s) de prescription/information GPU intersectent le point interrogé.")

    st.subheader("Ce que PRM refuse d’inventer")
    st.write(
        "Le prototype ne donne pas encore de couleur climatique 2050 ni de note de vulnérabilité "
        "de la végétation sans source ou photos. Ces modules seront ajoutés séparément."
    )

    if rerr:
        st.warning("Géorisques : l’appel adresse a échoué ou a nécessité un fallback communal. " + str(rerr))
    if uerrors:
        with st.expander("Erreurs techniques GPU (prototype)"):
            st.json(uerrors)

    st.subheader("Données techniques")
    with st.expander("Voir le JSON PRM"):
        st.json(assessment)

    raw = json.dumps(assessment, ensure_ascii=False, indent=2).encode("utf-8")
    st.download_button(
        "Télécharger le snapshot JSON",
        data=raw,
        file_name="prm_snapshot.json",
        mime="application/json",
        use_container_width=True,
    )

    st.caption(assessment["disclaimer"])
else:
    st.info("Le moteur ne s’exécute qu’après clic sur « Analyser cette propriété ».")

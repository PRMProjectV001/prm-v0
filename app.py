
import json
import math
import re
from datetime import datetime, timezone

import pandas as pd
import requests
import streamlit as st
from shapely.geometry import Point, shape, mapping
from shapely.ops import unary_union

st.set_page_config(page_title="PRM - Property Risk Management", page_icon="🏠", layout="wide")

GEOCODE_URL = "https://data.geopf.fr/geocodage/search"
CADASTRE_URL = "https://apicarto.ign.fr/api/cadastre/parcelle"
GPU_BASE = "https://apicarto.ign.fr/api/gpu"
GEORISQUES_REPORT_URL = "https://georisques.gouv.fr/api/v1/resultats_rapport_risque"
TIMEOUT = 20

# ---------- HTTP ----------
def api_get(url, params=None):
    try:
        r = requests.get(
            url, params=params, timeout=TIMEOUT,
            headers={"User-Agent": "PRM-V0.3/0.3"}
        )
        r.raise_for_status()
        return r.json(), None
    except Exception as e:
        return None, str(e)

# ---------- ADDRESS ----------
def geocode(address):
    data, err = api_get(GEOCODE_URL, {
        "q": address,
        "index": "address",
        "limit": 5,
        "autocomplete": "false",
    })
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
    }, None

def geojson_str(geom):
    return json.dumps(mapping(geom), separators=(",", ":"))

# ---------- CADASTRE ----------
def get_candidate_parcels(lon, lat):
    # Address points often fall on pavement. Query a small envelope around it.
    d = 0.00022
    poly = {
        "type": "Polygon",
        "coordinates": [[
            [lon-d, lat-d], [lon+d, lat-d], [lon+d, lat+d],
            [lon-d, lat+d], [lon-d, lat-d]
        ]]
    }
    data, err = api_get(CADASTRE_URL, {
        "geom": json.dumps(poly, separators=(",", ":")),
        "_limit": 60
    })
    feats = (data or {}).get("features", []) if data else []
    if not feats:
        return [], err

    pt = Point(lon, lat)
    scored = []
    for f in feats:
        try:
            g = shape(f["geometry"])
            p = f.get("properties", {})
            # Rough distance in degrees is sufficient for ranking a tiny local set.
            dist = g.distance(pt)
            centroid_dist = g.centroid.distance(pt)
            scored.append((dist, centroid_dist, f))
        except Exception:
            continue

    scored.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in scored[:20]], err

def parcel_id(f):
    p = f.get("properties", {})
    sec = str(p.get("section") or "").strip()
    num = str(p.get("numero") or p.get("numero_parcelle") or "").strip()
    if num.isdigit():
        num = num.zfill(4)
    return f"{sec} {num}".strip()

def parcel_row(f, lon, lat):
    p = f.get("properties", {})
    g = shape(f["geometry"])
    pt = Point(lon, lat)
    # degree-to-meter approximation for local ranking only
    approx_m = g.distance(pt) * 111000
    return {
        "Parcelle": parcel_id(f),
        "Surface m²": p.get("contenance"),
        "Distance approx. au point adresse": round(approx_m, 1),
        "ID cadastral": p.get("idu") or p.get("id"),
    }

def union_selected(features):
    geoms = [shape(f["geometry"]) for f in features]
    u = unary_union(geoms)
    # API Carto asks for reasonable geometry point counts
    return u.simplify(0.000001, preserve_topology=True)

# ---------- GPU / URBANISM ----------
def gpu_layer(layer, geom):
    data, err = api_get(
        f"{GPU_BASE}/{layer}",
        {"geom": geojson_str(geom), "_limit": 200}
    )
    feats = (data or {}).get("features", []) if data else []
    return feats, err

def get_urbanism(parcel_geom):
    layers = {
        "zones": "zone-urba",
        "prescriptions_surface": "prescription-surf",
        "prescriptions_line": "prescription-lin",
        "prescriptions_point": "prescription-pct",
        "infos_surface": "info-surf",
    }
    out, errors = {}, {}
    for key, layer in layers.items():
        feats, err = gpu_layer(layer, parcel_geom)
        out[key] = feats
        if err:
            errors[key] = err
    return out, errors

def extract_zone_labels(features):
    labels = []
    for f in features:
        p = f.get("properties", {})
        for key in ["libelle", "libelle_zone", "typezone", "type_zone", "nom", "name"]:
            v = p.get(key)
            if isinstance(v, str) and v.strip():
                labels.append(v.strip())
    labels = list(dict.fromkeys(labels))
    # Prefer specific labels such as UE over generic U.
    specific = [x for x in labels if len(x) > 1]
    return specific or labels

# ---------- GEORISQUES ----------
def get_georisques_report(lon, lat):
    return api_get(GEORISQUES_REPORT_URL, {"latlon": f"{lon},{lat}"})

def normalize_text(x):
    if x is None:
        return ""
    return " ".join(str(x).lower().split())

def subtree_matches(obj, aliases):
    """
    Return only subtrees whose key/path/name refers to the requested risk.
    This prevents text from another risk contaminating the classification.
    """
    aliases = [a.lower() for a in aliases]
    hits = []

    def walk(x, path=""):
        if isinstance(x, dict):
            # If current dict itself clearly names the risk, collect it.
            joined_keys = " ".join(str(k).lower() for k in x.keys())
            nameish = " ".join(
                normalize_text(x.get(k))
                for k in x.keys()
                if str(k).lower() in [
                    "libelle", "nom", "name", "type", "risque", "libelle_risque",
                    "code_risque", "titre", "title"
                ]
            )
            context = f"{path} {joined_keys} {nameish}"
            if any(a in context for a in aliases):
                hits.append(x)
            for k, v in x.items():
                walk(v, f"{path}/{str(k).lower()}")
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, f"{path}/{i}")

    walk(obj)
    # Deduplicate by serialized content
    unique = []
    seen = set()
    for h in hits:
        try:
            key = json.dumps(h, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            key = str(h)
        if key not in seen:
            seen.add(key)
            unique.append(h)
    return unique

def subtree_text(subtrees):
    vals = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                vals.append(str(k))
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, (str, int, float, bool)):
            vals.append(str(x))
    for s in subtrees:
        walk(s)
    return normalize_text(" | ".join(vals))

def find_scale(text, denominator):
    # Common phrases such as "3/3", "niveau 1", "zone 2"
    m = re.search(rf"\b([1-{denominator}])\s*/\s*{denominator}\b", text)
    if m:
        return int(m.group(1))
    for pat in [
        r"\bniveau\s*[:=]?\s*([1-5])\b",
        r"\bzone\s*[:=]?\s*([1-5])\b",
        r"\bcategorie\s*[:=]?\s*([1-5])\b",
        r"\bcatégorie\s*[:=]?\s*([1-5])\b",
    ]:
        m = re.search(pat, text)
        if m:
            n = int(m.group(1))
            if 1 <= n <= denominator:
                return n
    return None

def result(status, label, confidence="medium", evidence=None):
    return {
        "status": status,
        "label": label,
        "confidence": confidence,
        "evidence": evidence or []
    }

def classify_radon(report):
    subs = subtree_matches(report, ["radon"])
    text = subtree_text(subs)
    if not text:
        return result("⚪", "Niveau non récupéré", "low")
    n = find_scale(text, 3)
    if n == 1 or "potentiel radon faible" in text:
        return result("🟢", "Potentiel radon faible (1/3)", evidence=["Géorisques"])
    if n == 2:
        return result("🟠", "Potentiel radon intermédiaire (2/3)", evidence=["Géorisques"])
    if n == 3 or "potentiel radon élevé" in text or "potentiel radon eleve" in text:
        return result("🔴", "Potentiel radon élevé (3/3)", evidence=["Géorisques"])
    return result("⚪", "Radon mentionné, niveau non interprétable", "low")

def classify_seisme(report):
    subs = subtree_matches(report, ["séisme", "seisme", "sismique"])
    text = subtree_text(subs)
    if not text:
        return result("⚪", "Niveau non récupéré", "low")
    n = find_scale(text, 5)
    if n == 1:
        return result("🟢", "Sismicité très faible (1/5)", evidence=["Géorisques"])
    if n == 2:
        return result("🟢", "Sismicité faible (2/5)", evidence=["Géorisques"])
    if n in [3]:
        return result("🟠", f"Sismicité modérée ({n}/5)", evidence=["Géorisques"])
    if n in [4, 5]:
        return result("🔴", f"Sismicité forte ({n}/5)", evidence=["Géorisques"])
    return result("⚪", "Séisme mentionné, niveau non interprétable", "low")

def classify_argile(report):
    subs = subtree_matches(report, ["argile", "retrait gonflement", "gonflement"])
    text = subtree_text(subs)
    if not text:
        return result("⚪", "Niveau non récupéré", "low")
    n = find_scale(text, 3)
    if n == 1:
        return result("🟢", "Exposition argiles faible (1/3)", evidence=["Géorisques"])
    if n == 2:
        return result("🟠", "Exposition argiles moyenne (2/3)", evidence=["Géorisques"])
    if n == 3:
        return result("🔴", "Exposition argiles forte (3/3)", evidence=["Géorisques"])
    # Textual fallback only inside the argile subtree
    if "forte" in text or "fort" in text:
        return result("🔴", "Exposition argiles forte", "medium", ["Géorisques"])
    if "moyen" in text or "modéré" in text or "modere" in text:
        return result("🟠", "Exposition argiles intermédiaire", "medium", ["Géorisques"])
    if "faible" in text:
        return result("🟢", "Exposition argiles faible", "medium", ["Géorisques"])
    return result("⚪", "Argiles mentionnées, niveau non interprétable", "low")

def classify_inondation(report):
    subs = subtree_matches(report, ["inondation", "crue", "submersion", "remontée de nappe", "remontee de nappe"])
    text = subtree_text(subs)
    if not text:
        return result("⚪", "Aucune donnée exploitable récupérée", "low")
    # Address-level restrictive wording gets priority.
    strong_markers = [
        "affecte votre bien", "concerne votre bien", "zone rouge",
        "aléa fort", "alea fort", "risque fort"
    ]
    medium_markers = [
        "inondations de cave", "zone bleue", "aléa moyen", "alea moyen",
        "ppr", "territoire à risque important", "territoire a risque important"
    ]
    low_markers = ["non concerné", "non concerne", "faible", "hors zone"]
    if any(x in text for x in strong_markers):
        return result("🔴", "Exposition significative détectée", evidence=["Géorisques"])
    if any(x in text for x in low_markers):
        return result("🟢", "Faible exposition détectée", evidence=["Géorisques"])
    if any(x in text for x in medium_markers):
        return result("🟠", "Élément d'inondation à vérifier", evidence=["Géorisques"])
    return result("⚪", "Données d'inondation présentes, niveau à préciser", "low")

def classify_mouvements(report):
    subs = subtree_matches(report, ["mouvement de terrain", "cavité", "cavite", "effondrement", "glissement"])
    text = subtree_text(subs)
    if not text:
        return result("⚪", "Aucun événement précis récupéré", "low")
    # Avoid red without an explicit parcel/address effect.
    if "affecte votre bien" in text or "à mon adresse" in text and any(
        x in text for x in ["glissement", "effondrement", "cavité", "cavite"]
    ):
        return result("🟠", "Événement / aléa local à examiner", evidence=["Géorisques"])
    if "catastrophe naturelle" in text or "commune" in text:
        return result("⚪", "Historique communal détecté, exposition parcellaire non prouvée", "low")
    return result("⚪", "Donnée présente, exposition parcellaire à préciser", "low")

def classify_tech(report):
    subs = subtree_matches(report, ["seveso", "installation classée", "installation classee", "icpe", "canalisation", "nucléaire", "nucleaire"])
    text = subtree_text(subs)
    if not text:
        return result("🟢", "Aucun signal technologique majeur extrait", "medium", ["Géorisques"])
    if "seveso seuil haut" in text and ("moins de" in text or "à mon adresse" in text):
        return result("🔴", "Installation SEVESO seuil haut à proximité", evidence=["Géorisques"])
    if "seveso seuil bas" in text or "canalisation" in text or "icpe" in text:
        return result("🟠", "Risque technologique à vérifier", evidence=["Géorisques"])
    return result("⚪", "Donnée technologique présente, importance à préciser", "low")

# ---------- UI ----------
def risk_card(name, item):
    st.markdown(
        f"""
        <div style="border:1px solid #e5e7eb;border-radius:14px;padding:14px 16px;margin:7px 0;background:white">
          <div style="font-size:1.05rem;font-weight:700">{item['status']} {name}</div>
          <div style="color:#4b5563;margin-top:4px">{item['label']}</div>
          <div style="font-size:.78rem;color:#9ca3af;margin-top:6px">
            Confiance : {item['confidence']}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.markdown("""
<style>
.block-container {max-width:1120px;padding-top:2rem}
h1 {letter-spacing:-.03em}
.hero {padding:24px;border-radius:20px;background:#111827;color:white;margin-bottom:18px}
.kicker {font-size:.8rem;letter-spacing:.12em;text-transform:uppercase;color:#9ca3af}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="kicker">Property Risk Management · Prototype V0.3</div>', unsafe_allow_html=True)
st.title("Avant d’acheter, voyez les risques.")
st.write("V0.3 : choix des parcelles candidates, risques isolés par catégorie, urbanisme analysé sur la parcelle.")

with st.form("address_form"):
    address = st.text_input("Adresse", value="27 rue des Jardins 92380 Garches")
    address_submit = st.form_submit_button("Identifier la propriété", use_container_width=True)

if address_submit:
    geo, err = geocode(address)
    if not geo:
        st.error(f"Adresse introuvable : {err}")
    else:
        candidates, cerr = get_candidate_parcels(geo["lon"], geo["lat"])
        st.session_state["geo"] = geo
        st.session_state["candidates"] = candidates
        st.session_state["address"] = address
        if cerr:
            st.session_state["cadastre_warning"] = cerr

if "geo" in st.session_state and st.session_state.get("candidates"):
    geo = st.session_state["geo"]
    candidates = st.session_state["candidates"]

    st.subheader("1. Parcelles candidates")
    st.caption(
        "Le point d'adresse peut tomber sur le trottoir. PRM propose les parcelles les plus proches. "
        "Vérifiez la sélection avant de lancer l'analyse."
    )

    rows = [parcel_row(f, geo["lon"], geo["lat"]) for f in candidates]
    df = pd.DataFrame(rows)
    st.dataframe(df, hide_index=True, use_container_width=True)

    labels = [parcel_id(f) for f in candidates]
    # Default: two closest candidates, which also supports houses spanning two parcels.
    default = labels[:2] if len(labels) >= 2 else labels[:1]

    selected_labels = st.multiselect(
        "Parcelle(s) à analyser",
        options=labels,
        default=default,
        help="Pour une maison sur plusieurs parcelles, sélectionnez-les toutes."
    )

    if st.button("Analyser les parcelles sélectionnées", use_container_width=True):
        selected = [f for f in candidates if parcel_id(f) in selected_labels]
        if not selected:
            st.error("Sélectionnez au moins une parcelle.")
            st.stop()

        with st.spinner("Analyse PRM V0.3…"):
            parcel_geom = union_selected(selected)
            urbanism, uerrors = get_urbanism(parcel_geom)
            report, rerr = get_georisques_report(geo["lon"], geo["lat"])

        zones = extract_zone_labels(urbanism.get("zones", []))
        urbanism_item = (
            result("🟠", "Zonage détecté : " + ", ".join(zones[:3]), "high", ["GPU / IGN"])
            if zones else result("⚪", "Zonage non récupéré", "low")
        )

        categories = {
            "Inondation / nappe": classify_inondation(report),
            "Argiles / sols": classify_argile(report),
            "Mouvements / cavités": classify_mouvements(report),
            "Séisme": classify_seisme(report),
            "Radon": classify_radon(report),
            "Risques technologiques": classify_tech(report),
            "Urbanisme": urbanism_item,
            "Climat 2050": result("⚪", "Module officiel à connecter", "none"),
            "Végétation / vulnérabilité feu": result("⚪", "Photos requises", "none"),
        }

        reds = sum(v["status"] == "🔴" for v in categories.values())
        oranges = sum(v["status"] == "🟠" for v in categories.values())
        greys = sum(v["status"] == "⚪" for v in categories.values())

        if reds:
            global_status, global_label = "🔴", "VIGILANCE FORTE"
        elif oranges >= 2:
            global_status, global_label = "🟠", "VIGILANCE"
        elif oranges == 1:
            global_status, global_label = "🟠", "VIGILANCE LIMITÉE"
        else:
            global_status, global_label = "⚪", "DONNÉES À COMPLÉTER"

        st.markdown(
            f"""
            <div class="hero">
              <div style="font-size:.85rem;color:#9ca3af">PRM SNAPSHOT V0.3</div>
              <div style="font-size:1.55rem;font-weight:800;margin-top:5px">{geo['label']}</div>
              <div style="font-size:1rem;color:#d1d5db;margin-top:4px">Parcelle(s) : {", ".join(selected_labels)}</div>
              <div style="font-size:2.1rem;font-weight:800;margin-top:14px">{global_status} {global_label}</div>
              <div style="color:#d1d5db;margin-top:5px">{oranges} vigilances · {reds} fortes vigilances · {greys} points non évalués</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        c1, c2 = st.columns(2)
        with c1:
            st.subheader("Localisation")
            st.map(pd.DataFrame([{"lat": geo["lat"], "lon": geo["lon"]}]), zoom=17)
        with c2:
            st.subheader("Périmètre analysé")
            selected_rows = [parcel_row(f, geo["lon"], geo["lat"]) for f in selected]
            st.dataframe(pd.DataFrame(selected_rows), hide_index=True, use_container_width=True)
            st.caption("Urbanisme interrogé sur la géométrie des parcelles sélectionnées.")

        st.subheader("2. Feux PRM")
        cols = st.columns(2)
        for i, (name, item) in enumerate(categories.items()):
            with cols[i % 2]:
                risk_card(name, item)

        st.subheader("3. Urbanisme parcellaire")
        if zones:
            st.write("**Zonage GPU :**", " · ".join(zones[:8]))
        else:
            st.info("Aucun zonage exploitable récupéré.")
        n_presc = (
            len(urbanism.get("prescriptions_surface", []))
            + len(urbanism.get("prescriptions_line", []))
            + len(urbanism.get("prescriptions_point", []))
            + len(urbanism.get("infos_surface", []))
        )
        if n_presc == 0:
            st.write("Aucune prescription/information GPU particulière détectée sur le périmètre analysé.")
        else:
            st.warning(f"{n_presc} prescription(s) ou information(s) GPU intersectent les parcelles sélectionnées.")

        st.subheader("4. Garde-fous PRM")
        st.write(
            "V0.3 ne met plus un risque en rouge à cause d'un mot trouvé dans une autre section. "
            "Radon, séisme et argiles utilisent leurs propres sous-arbres de données et leurs échelles dédiées. "
            "Quand le niveau n'est pas interprétable, PRM affiche ⚪ plutôt que d'inventer."
        )

        snapshot = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "address": geo,
            "selected_parcels": selected_rows,
            "risk_categories": categories,
            "urbanism": {
                "zones": zones,
                "prescription_count": n_presc
            },
            "warnings": {
                "georisques": rerr,
                "gpu": uerrors
            }
        }

        with st.expander("Voir les données techniques PRM"):
            st.json(snapshot)

        st.download_button(
            "Télécharger le snapshot JSON V0.3",
            data=json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8"),
            file_name="prm_snapshot_v03.json",
            mime="application/json",
            use_container_width=True,
        )

        st.caption(
            "Prototype d'aide à la décision. Ne remplace pas un état des risques officiel, une étude géotechnique, "
            "un certificat d'urbanisme, une expertise bâtiment ou un conseil professionnel."
        )

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent météo d'irrigation — Jardin de Mme Torchia, Bernay (27300)
================================================================
Calcule un bilan hydrique hebdomadaire (évapotranspiration ET0 FAO-56 + pluie),
via l'API gratuite Open-Meteo, et envoie chaque vendredi soir par e-mail une
préconisation d'arrosage pour chacun de vos végétaux.

Aucune dépendance à installer : uniquement la bibliothèque standard de Python 3.

Utilisation :
    python agent_arrosage.py          # exécution normale (envoie l'e-mail)
    python agent_arrosage.py --test   # mode démo hors-ligne (données fictives)

Les identifiants e-mail sont lus dans des variables d'environnement (secrets) :
    SMTP_USER, SMTP_PASS, EMAIL_TO  (voir README.md)
Si SMTP_USER/SMTP_PASS sont absents, le rapport est simplement affiché à l'écran.
"""

import os
import sys
import ssl
import json
import smtplib
import urllib.request
import urllib.parse
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ===========================================================================
# 1. LOCALISATION
# ===========================================================================
LATITUDE = 49.09979
LONGITUDE = 0.59961
TIMEZONE = "Europe/Paris"

# Date de plantation commune (mai 2024). Le « facteur d'établissement » est
# recalculé à chaque exécution : plus les plants vieillissent, moins ils ont
# besoin d'arrosage. Passé 3 ans, ils sont considérés comme établis.
DATE_PLANTATION = date(2024, 5, 15)

# ===========================================================================
# 2. VOS VÉGÉTAUX
# ---------------------------------------------------------------------------
#   kc          : coefficient cultural (besoin en eau relatif du végétal)
#   expo        : correction d'exposition  (sud > 1 = sèche vite ; nord < 1)
#   seuil_mm    : déficit hebdomadaire (mm) au-delà duquel on arrose
#   surface_m2  : surface de sol à humidifier — sert à convertir les mm en
#                 litres (1 mm d'eau = 1 litre par m²).
#
#   >>> À ajuster : augmentez `surface_m2` au fur et à mesure que les sujets
#       grossissent (frondaison plus large = plus de litres).
# ===========================================================================
PLANTES = [
    # --- Assoiffées : à garder fraîches, même une fois établies ---
    {"nom": "Hydrangea",                    "kc": 1.00, "expo": 1.25, "seuil_mm": 8,  "surface_m2": 0.6, "groupe": "Assoiffées"},
    {"nom": "Acorus 'Ogon'",                "kc": 1.00, "expo": 1.25, "seuil_mm": 8,  "surface_m2": 0.3, "groupe": "Assoiffées"},

    # --- Besoins moyens (surtout tant qu'ils sont jeunes) ---
    {"nom": "Amélanchier",                  "kc": 0.80, "expo": 1.25, "seuil_mm": 15, "surface_m2": 1.0, "groupe": "Moyens"},
    {"nom": "Cornus Kousa",                 "kc": 0.80, "expo": 1.00, "seuil_mm": 15, "surface_m2": 1.0, "groupe": "Moyens"},
    {"nom": "Magnolia de Loebner",          "kc": 0.80, "expo": 1.05, "seuil_mm": 15, "surface_m2": 1.0, "groupe": "Moyens"},
    {"nom": "Acer Palmatum vert (nord)",    "kc": 0.80, "expo": 0.80, "seuil_mm": 15, "surface_m2": 1.0, "groupe": "Moyens"},
    {"nom": "Pommier",                      "kc": 0.80, "expo": 1.00, "seuil_mm": 15, "surface_m2": 1.5, "groupe": "Moyens"},
    {"nom": "Cerisier",                     "kc": 0.80, "expo": 1.00, "seuil_mm": 15, "surface_m2": 1.5, "groupe": "Moyens"},

    # --- Résistants à la sécheresse une fois établis ---
    {"nom": "Photinia",                     "kc": 0.50, "expo": 1.00, "seuil_mm": 22, "surface_m2": 0.4, "groupe": "Résistants"},
    {"nom": "Troène de Chine",              "kc": 0.50, "expo": 1.00, "seuil_mm": 22, "surface_m2": 0.4, "groupe": "Résistants"},
    {"nom": "Laurier du Portugal",          "kc": 0.50, "expo": 1.00, "seuil_mm": 22, "surface_m2": 0.4, "groupe": "Résistants"},
    {"nom": "Pittosporum",                  "kc": 0.50, "expo": 1.25, "seuil_mm": 22, "surface_m2": 0.5, "groupe": "Résistants"},
    {"nom": "Oranger du Mexique (Choisya)", "kc": 0.50, "expo": 1.25, "seuil_mm": 22, "surface_m2": 0.5, "groupe": "Résistants"},
    {"nom": "Agapanthe",                    "kc": 0.50, "expo": 1.25, "seuil_mm": 22, "surface_m2": 0.3, "groupe": "Résistants"},

    # --- Grand sujet en cours d'établissement ---
    {"nom": "Hêtre à feuilles de fougères", "kc": 0.70, "expo": 1.15, "seuil_mm": 18, "surface_m2": 1.2, "groupe": "Arbre"},
]

# Seuils divers
SEUIL_CANICULE = 30.0   # °C (température max prévue) au-delà duquel on alerte


# ===========================================================================
# 3. RÉCUPÉRATION DES DONNÉES MÉTÉO (Open-Meteo, gratuit, sans clé)
# ===========================================================================
def recuperer_meteo():
    """Renvoie un dict {date_iso: {'et0':mm, 'pluie':mm, 'tmax':°C}}."""
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "daily": "et0_fao_evapotranspiration,precipitation_sum,temperature_2m_max",
        "past_days": 7,        # 7 jours écoulés (le bilan récent)
        "forecast_days": 8,    # aujourd'hui + 7 jours de prévision
        "timezone": TIMEZONE,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as reponse:
        data = json.loads(reponse.read().decode("utf-8"))

    d = data["daily"]
    resultat = {}
    for i, jour in enumerate(d["time"]):
        resultat[jour] = {
            "et0": (d["et0_fao_evapotranspiration"][i] or 0.0),
            "pluie": (d["precipitation_sum"][i] or 0.0),
            "tmax": (d["temperature_2m_max"][i] if d["temperature_2m_max"][i] is not None else 0.0),
        }
    return resultat


# ===========================================================================
# 4. CALCUL DU BILAN HYDRIQUE
# ===========================================================================
def facteur_etablissement(aujourdhui):
    """Plus les plants sont jeunes, plus ils ont besoin d'un suivi rapproché."""
    ans = (aujourdhui - DATE_PLANTATION).days / 365.25
    if ans < 1:
        return 1.5, "1re année (installation critique)"
    elif ans < 3:
        return 1.2, f"{ans:.1f} ans (encore en installation)"
    else:
        return 1.0, f"{ans:.1f} ans (établi)"


def somme(meteo, jours, champ):
    return sum(meteo[j][champ] for j in jours if j in meteo)


def maximum(meteo, jours, champ):
    valeurs = [meteo[j][champ] for j in jours if j in meteo]
    return max(valeurs) if valeurs else 0.0


def analyser(meteo, aujourdhui):
    """Construit la synthèse hebdomadaire + une préconisation par plante."""
    fe, fe_txt = facteur_etablissement(aujourdhui)

    j_passes = [(aujourdhui - timedelta(days=k)).isoformat() for k in range(1, 8)]   # 7 jours écoulés
    j_futurs = [(aujourdhui + timedelta(days=k)).isoformat() for k in range(0, 7)]   # 7 jours à venir
    j_futurs3 = [(aujourdhui + timedelta(days=k)).isoformat() for k in range(0, 3)]  # 3 jours à venir

    et0_passe = somme(meteo, j_passes, "et0")
    pluie_passee = somme(meteo, j_passes, "pluie")
    pluie_future = somme(meteo, j_futurs, "pluie")
    pluie_3j = somme(meteo, j_futurs3, "pluie")
    tmax_future = maximum(meteo, j_futurs, "tmax")

    synthese = {
        "et0_passe": et0_passe,
        "pluie_passee": pluie_passee,
        "pluie_future": pluie_future,
        "tmax_future": tmax_future,
        "canicule": tmax_future >= SEUIL_CANICULE,
        "etablissement": fe_txt,
    }

    preco = []
    for p in PLANTES:
        # Déficit physique réel (mm) sur la semaine écoulée
        deficit_phys = max(0.0, p["kc"] * et0_passe - pluie_passee)
        # Déficit "de décision", majoré par l'exposition et la jeunesse du plant
        deficit_dec = deficit_phys * p["expo"] * fe

        if deficit_dec < p["seuil_mm"]:
            verdict, litres, note = "RAS", 0, "pluie/fraîcheur suffisantes"
        elif pluie_3j >= deficit_phys:
            verdict, litres, note = "REPORTER", 0, f"{pluie_3j:.0f} mm de pluie attendus sous 3 j"
        else:
            litres = round(deficit_phys * p["expo"] * p["surface_m2"])
            verdict, note = "ARROSER", f"déficit ~{deficit_dec:.0f} mm"

        preco.append({
            "nom": p["nom"], "groupe": p["groupe"],
            "verdict": verdict, "litres": litres, "note": note,
        })

    # On met les "à arroser" en tête
    ordre = {"ARROSER": 0, "REPORTER": 1, "RAS": 2}
    preco.sort(key=lambda x: (ordre[x["verdict"]], -x["litres"]))
    return synthese, preco


# ===========================================================================
# 5. MISE EN FORME DU RAPPORT
# ===========================================================================
ICONE = {"ARROSER": "💧", "REPORTER": "⏸️", "RAS": "✅"}


def construire_texte(synthese, preco, aujourdhui):
    lignes = []
    lignes.append(f"PRÉCONISATIONS D'ARROSAGE — {aujourdhui.strftime('%d/%m/%Y')}")
    lignes.append("Jardin de Bernay (27300)")
    lignes.append("")
    lignes.append("Bilan de la semaine écoulée :")
    lignes.append(f"  • Évapotranspiration (ET0) : {synthese['et0_passe']:.0f} mm")
    lignes.append(f"  • Pluie tombée            : {synthese['pluie_passee']:.0f} mm")
    lignes.append(f"  • Pluie prévue (7 j)      : {synthese['pluie_future']:.0f} mm")
    lignes.append(f"  • Plants                  : {synthese['etablissement']}")
    if synthese["canicule"]:
        lignes.append(f"  ⚠️  CHALEUR : jusqu'à {synthese['tmax_future']:.0f} °C prévus — arrosez tôt le matin ou le soir.")
    lignes.append("")
    a_arroser = [x for x in preco if x["verdict"] == "ARROSER"]
    if not a_arroser:
        lignes.append("Aucun arrosage nécessaire cette semaine. 🌿")
        lignes.append("")
    for x in preco:
        base = f"{ICONE[x['verdict']]} {x['nom']} — "
        if x["verdict"] == "ARROSER":
            base += f"arroser ~{x['litres']} L ({x['note']})"
        elif x["verdict"] == "REPORTER":
            base += f"reporter ({x['note']})"
        else:
            base += f"rien à faire ({x['note']})"
        lignes.append(base)
    lignes.append("")
    lignes.append("— Agent météo (données Open-Meteo, méthode FAO-56). "
                  "Ces valeurs sont indicatives : ajustez selon l'état réel du sol et des feuilles.")
    return "\n".join(lignes)


def construire_html(synthese, preco, aujourdhui):
    couleur = {"ARROSER": "#1565c0", "REPORTER": "#8d6e63", "RAS": "#2e7d32"}
    lignes_html = ""
    for x in preco:
        if x["verdict"] == "ARROSER":
            action = f"<b>arroser ~{x['litres']} L</b> <span style='color:#777'>({x['note']})</span>"
        elif x["verdict"] == "REPORTER":
            action = f"reporter <span style='color:#777'>({x['note']})</span>"
        else:
            action = f"rien à faire <span style='color:#777'>({x['note']})</span>"
        lignes_html += (
            f"<tr>"
            f"<td style='padding:6px 10px;font-size:18px'>{ICONE[x['verdict']]}</td>"
            f"<td style='padding:6px 10px'>{x['nom']}</td>"
            f"<td style='padding:6px 10px;color:{couleur[x['verdict']]}'>{action}</td>"
            f"</tr>"
        )
    alerte = ""
    if synthese["canicule"]:
        alerte = (f"<p style='background:#fff3e0;border-left:4px solid #ef6c00;"
                  f"padding:10px 14px;margin:12px 0'>⚠️ <b>Chaleur</b> : jusqu'à "
                  f"{synthese['tmax_future']:.0f} °C prévus. Arrosez tôt le matin ou le soir, "
                  f"jamais en plein soleil.</p>")
    return f"""\
<html><body style="font-family:Arial,Helvetica,sans-serif;color:#222;max-width:640px;margin:auto">
  <h2 style="margin-bottom:2px">💧 Préconisations d'arrosage</h2>
  <p style="color:#777;margin-top:0">Jardin de Bernay — {aujourdhui.strftime('%d/%m/%Y')}</p>
  <div style="background:#f5f7fa;border-radius:8px;padding:12px 16px;font-size:14px">
    <b>Semaine écoulée :</b> évapotranspiration {synthese['et0_passe']:.0f} mm,
    pluie {synthese['pluie_passee']:.0f} mm — <b>pluie prévue (7 j) :</b>
    {synthese['pluie_future']:.0f} mm<br>
    <span style="color:#777">Plants : {synthese['etablissement']}</span>
  </div>
  {alerte}
  <table style="border-collapse:collapse;width:100%;margin-top:12px">{lignes_html}</table>
  <p style="color:#999;font-size:12px;margin-top:18px">
    Données Open-Meteo · méthode bilan hydrique FAO-56.
    Valeurs indicatives : vérifiez l'état réel du sol et des feuilles.
  </p>
</body></html>"""


# ===========================================================================
# 6. ENVOI DE L'E-MAIL
# ===========================================================================
def envoyer_email(sujet, texte, html):
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    email_to = os.environ.get("EMAIL_TO", "giocondotorchia@gmail.com")
    email_from = os.environ.get("EMAIL_FROM", smtp_user)

    if not smtp_user or not smtp_pass:
        print("ℹ️  SMTP_USER/SMTP_PASS absents : mode démonstration, l'e-mail n'est pas envoyé.\n")
        print(texte)
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = sujet
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText(texte, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    contexte = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as serveur:
        serveur.starttls(context=contexte)
        serveur.login(smtp_user, smtp_pass)
        serveur.sendmail(email_from, [email_to], msg.as_string())
    print(f"✅ E-mail envoyé à {email_to}")


# ===========================================================================
# 7. PROGRAMME PRINCIPAL
# ===========================================================================
def meteo_fictive(aujourdhui):
    """Jeu de données de démonstration (semaine chaude et sèche)."""
    m = {}
    for k in range(-7, 8):
        j = (aujourdhui + timedelta(days=k)).isoformat()
        m[j] = {"et0": 4.5, "pluie": 0.0 if k > -3 else 2.0, "tmax": 31.0}
    return m


def main():
    mode_test = "--test" in sys.argv
    aujourdhui = date.today()

    if mode_test:
        meteo = meteo_fictive(aujourdhui)
    else:
        try:
            meteo = recuperer_meteo()
        except Exception as e:  # noqa: BLE001
            print(f"❌ Impossible de récupérer la météo : {e}")
            sys.exit(1)

    synthese, preco = analyser(meteo, aujourdhui)
    texte = construire_texte(synthese, preco, aujourdhui)
    html = construire_html(synthese, preco, aujourdhui)
    sujet = f"💧 Arrosage du jardin — {aujourdhui.strftime('%d/%m/%Y')}"

    if mode_test:
        print(texte)
    else:
        envoyer_email(sujet, texte, html)


if __name__ == "__main__":
    main()

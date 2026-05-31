
# ─────────────────────────────────────────────
#  IMPORTS (tous en haut, ordre propre)
# ─────────────────────────────────────────────
import os
import time
import random
import string
import hashlib
import requests as http_req          # appels HTTP vers CinetPay
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
load_dotenv()


app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────
# CONFIGURATION BASE DE DONNÉES (CORRIGÉE)
# ─────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("DB_HOST"),
    "port":     int(os.getenv("DB_PORT")),
    "dbname":   os.getenv("DB_NAME"),
    "user":     os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
}

# ─────────────────────────────────────────────
# CONFIGURATION CINETPAY (CORRIGÉE)
# ─────────────────────────────────────────────
CINETPAY_API_KEY = os.getenv("CINETPAY_API_KEY", "")
CINETPAY_SITE_ID = os.getenv("CINETPAY_SITE_ID", "")
CINETPAY_PAY_URL = "https://api-checkout.cinetpay.com/v2/payment"
CINETPAY_CHK_URL = "https://api-checkout.cinetpay.com/v2/payment/check"

# Base URL (à configurer dans .env)
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://9ee7b9cc462d4c4a-212-32-207-98.serveousercontent.com")

# URLs de redirection construites dynamiquement
RETURN_URL = f"{APP_BASE_URL}/paiement/succes"
CANCEL_URL = f"{APP_BASE_URL}/paiement/echec"
NOTIFY_URL = f"{APP_BASE_URL}/api/paiement/notification"

# Vérification que les clés sont présentes
if not CINETPAY_API_KEY or not CINETPAY_SITE_ID:
    print("⚠️ ATTENTION: Les clés CinetPay ne sont pas configurées dans le fichier .env")

print(f"✅ Configuration chargée:")
print(f"   - DB: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")
print(f"   - CinetPay: {CINETPAY_PAY_URL}")
print(f"   - Base URL: {APP_BASE_URL}")

# ─────────────────────────────────────────────
#  HELPERS DB
# ─────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(**DB_CONFIG)
 
def hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()
 
def gen_code() -> str:
    """Génère un code de suivi unique → FAB-XXXXXX"""
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"FAB-{suffix}"
 
def gen_transaction_id() -> str:
    """Génère un ID de transaction unique pour CinetPay"""
    return f"FABLA-{int(time.time())}-{random.randint(1000, 9999)}"
 
def row(cursor):
    """Retourne un dict depuis le dernier fetchone()"""
    r = cursor.fetchone()
    return dict(r) if r else None
 
def rows(cursor):
    return [dict(r) for r in cursor.fetchall()]
 
def safe_user(u: dict) -> dict:
    """Retire le pin_hash avant envoi au client"""
    if u:
        u = dict(u)
        u.pop("pin_hash", None)
    return u
 
def add_suivi(conn, colis_id: int, statut: str, message: str, role: str = "system"):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO suivi_statuts (colis_id, statut, message, auteur_role) VALUES (%s,%s,%s,%s)",
        (colis_id, statut, message, role),
    )
    cur.close()
 
# ─────────────────────────────────────────────
#  INITIALISATION DES TABLES
# ─────────────────────────────────────────────
def init_db():
    conn = get_conn()
    cur  = conn.cursor()
 
    # ── Utilisateurs (3 rôles) ──────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS utilisateurs (
            id          SERIAL PRIMARY KEY,
            telephone   VARCHAR(20) UNIQUE NOT NULL,
            role        VARCHAR(10) NOT NULL DEFAULT 'client',
            pin_hash    TEXT,
            nom         VARCHAR(100),
            actif       BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMP DEFAULT NOW()
        );
    """)
 
    # ── Colis ───────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS colis (
            id                      SERIAL PRIMARY KEY,
            code_suivi              VARCHAR(12) UNIQUE NOT NULL,
            type_service            VARCHAR(20) NOT NULL,
            client_id               INTEGER REFERENCES utilisateurs(id),
            telephone_client        VARCHAR(20) NOT NULL,
            livreur_id              INTEGER REFERENCES utilisateurs(id),
 
            -- Récupération
            lieu_recuperation       TEXT,
            description_colis       TEXT,
 
            -- Achat
            article                 TEXT,
            boutique                TEXT,
            budget_article          NUMERIC(10,2),
 
            -- Livraison
            nom_destinataire        TEXT,
            telephone_dest          VARCHAR(20),
            adresse_livraison       TEXT,
            description_envoi       TEXT,
 
            -- Commun
            adresse_client          TEXT,
            adresse_destination_id  INTEGER,
            note                    TEXT,
 
            -- Paiement & frais
            frais                   NUMERIC(10,2) DEFAULT 0,
            frais_livraison         NUMERIC(10,2) DEFAULT 0,
            frais_service           NUMERIC(10,2) DEFAULT 0,
            transaction_id          VARCHAR(60),
            paiement_statut         VARCHAR(20) DEFAULT 'en_attente',
 
            -- Statut
            statut                  VARCHAR(30) DEFAULT 'en_attente',
            created_at              TIMESTAMP DEFAULT NOW(),
            updated_at              TIMESTAMP DEFAULT NOW()
        );
    """)
 
    # ── Historique des statuts ───────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS suivi_statuts (
            id          SERIAL PRIMARY KEY,
            colis_id    INTEGER REFERENCES colis(id),
            statut      VARCHAR(50) NOT NULL,
            message     TEXT,
            auteur_role VARCHAR(10),
            created_at  TIMESTAMP DEFAULT NOW()
        );
    """)
 
    # ── Transactions CinetPay ────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id              SERIAL PRIMARY KEY,
            transaction_id  VARCHAR(60) UNIQUE NOT NULL,
            telephone       VARCHAR(20) NOT NULL,
            montant         NUMERIC(10,2) NOT NULL,
            description     TEXT,
            statut          VARCHAR(20) DEFAULT 'en_attente',
            colis_id        INTEGER REFERENCES colis(id),
            cinetpay_ref    TEXT,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW()
        );
    """)
 
    # ── Admin par défaut ─────────────────────────────────────────
    cur.execute("SELECT id FROM utilisateurs WHERE role='admin' LIMIT 1")
    if not cur.fetchone():
        cur.execute("""
            INSERT INTO utilisateurs (telephone, role, pin_hash, nom)
            VALUES ('0710069791', 'admin', %s, 'Super Admin')
        """, (hash_pin("1234"),))
        print("✅ Admin créé — tél: 0710069791 / PIN: 1234")
 
    conn.commit()
    cur.close()
    conn.close()
    print("✅ Base de données initialisée.")
 
 
# ═══════════════════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════════════════
 
@app.route("/api/auth/client", methods=["POST"])
def auth_client():
    d   = request.get_json()
    tel = d.get("telephone", "").strip()
    if not tel or len(tel) < 4:
        return jsonify({"error": "Numéro invalide"}), 400
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM utilisateurs WHERE telephone=%s AND role='client'", (tel,))
        user = row(cur)
        if not user:
            cur.execute(
                "INSERT INTO utilisateurs (telephone, role) VALUES (%s,'client') RETURNING *",
                (tel,)
            )
            user = row(cur)
            conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True, "user": safe_user(user)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route("/api/auth/livreur", methods=["POST"])
def auth_livreur():
    d   = request.get_json()
    tel = d.get("telephone", "").strip()
    pin = d.get("pin", "").strip()
    if not tel or not pin:
        return jsonify({"error": "Téléphone et PIN requis"}), 400
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM utilisateurs WHERE telephone=%s AND role='livreur'", (tel,))
        user = row(cur)
        cur.close(); conn.close()
        if not user:       return jsonify({"error": "Livreur introuvable."}), 404
        if not user["actif"]: return jsonify({"error": "Compte désactivé."}), 403
        if user["pin_hash"] != hash_pin(pin):
            return jsonify({"error": "PIN incorrect"}), 401
        return jsonify({"success": True, "user": safe_user(user)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route("/api/auth/admin", methods=["POST"])
def auth_admin():
    d   = request.get_json()
    tel = d.get("telephone", "").strip()
    pin = d.get("pin", "").strip()
    if not tel or not pin:
        return jsonify({"error": "Téléphone et PIN requis"}), 400
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM utilisateurs WHERE telephone=%s AND role='admin'", (tel,))
        user = row(cur)
        cur.close(); conn.close()
        if not user:
            return jsonify({"error": "Admin introuvable"}), 404
        if user["pin_hash"] != hash_pin(pin):
            return jsonify({"error": "PIN incorrect"}), 401
        return jsonify({"success": True, "user": safe_user(user)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
# ═══════════════════════════════════════════════════════════
#  PAIEMENT CINETPAY  ←  COMPLET
# ═══════════════════════════════════════════════════════════
 
@app.route("/api/paiement/initier", methods=["POST"])
def initier_paiement():
    """
    Étape 1 — Le frontend demande l'URL de paiement CinetPay.
    Retourne payment_url + transaction_id.
    Le frontend ouvre la WebView sur payment_url.
    """
    d           = request.get_json()
    montant     = d.get("montant")
    telephone   = d.get("telephone", "").strip()
    description = d.get("description", "Paiement FABLA")
 
    if not montant or float(montant) <= 0:
        return jsonify({"error": "Montant invalide"}), 400
    if not telephone:
        return jsonify({"error": "Téléphone requis"}), 400
 
    # CinetPay exige un minimum de 100 XOF
    montant_final  = max(int(float(montant)), 100)
    transaction_id = gen_transaction_id()
 
    # ── Champs client (FABLA n'a que le téléphone → on construit des valeurs par défaut) ──
    # CinetPay v2 exige : customer_name, customer_surname, customer_email, customer_phone_number
    # + customer_address, customer_city, customer_country, customer_state, customer_zip
    nom_client     = d.get("nom", "Client")                          # optionnel depuis le frontend
    prenom_client  = d.get("prenom", "FABLA")
    email_client   = d.get("email", f"{telephone}@fabla.ci")        # email fictif basé sur le tel
    # Nettoyer le numéro : CinetPay préfère le format international sans le "+"
    tel_intl = telephone.lstrip("+").replace(" ", "")
    if tel_intl.startswith("0"):                                      # 07XXXXXXXX → 2250 7XXXXXXXX
        tel_intl = "225" + tel_intl[1:]
 
    payload = {
        # ── Identification API ──
        "apikey":   CINETPAY_API_KEY,
        "site_id":  CINETPAY_SITE_ID,
 
        # ── Transaction ──
        "transaction_id": transaction_id,
        "amount":         montant_final,
        "currency":       "XOF",
        "description":    description[:100],                          # max 100 car.
        "channels":       "ALL",                                      # Mobile Money + carte
        "lang":           "fr",
        "metadata":       telephone,
 
        # ── URLs de retour (détectées par la WebView React Native) ──
        "return_url": RETURN_URL,
        "cancel_url": CANCEL_URL,
        "notify_url": NOTIFY_URL,
 
        # ── Informations client OBLIGATOIRES pour CinetPay v2 ──
        "customer_name":           nom_client,
        "customer_surname":        prenom_client,
        "customer_email":          email_client,
        "customer_phone_number":   tel_intl,
        "customer_address":        "Assinie-Mafia",
        "customer_city":           "Assinie",
        "customer_country":        "CI",                              # code ISO Côte d'Ivoire
        "customer_state":          "CI",
        "customer_zip":            "00225",
    }
 
    try:
        resp   = http_req.post(CINETPAY_PAY_URL, json=payload, timeout=15)
        result = resp.json()
        code   = str(result.get("code", ""))
 
        if code == "201":
            payment_url = result["data"]["payment_url"]
 
            # Enregistrer la transaction en base (statut: en_attente)
            conn = get_conn()
            cur  = conn.cursor()
            cur.execute("""
                INSERT INTO transactions (transaction_id, telephone, montant, description, statut)
                VALUES (%s, %s, %s, %s, 'en_attente')
                ON CONFLICT (transaction_id) DO NOTHING
            """, (transaction_id, telephone, montant_final, description))
            conn.commit()
            cur.close()
            conn.close()
 
            return jsonify({
                "success":        True,
                "payment_url":    payment_url,
                "transaction_id": transaction_id,
                "montant":        montant_final,
            }), 200
 
        else:
            # Log complet pour le débogage
            print(f"[CINETPAY ERROR] code={code} | message={result.get('message')} | data={result.get('data')}")
            return jsonify({
                "error":   result.get("message", "Erreur CinetPay"),
                "code":    code,
                "data":    result.get("data"),
                "detail":  result,
            }), 400
 
    except http_req.exceptions.Timeout:
        return jsonify({"error": "CinetPay ne répond pas (timeout). Réessayez."}), 504
    except Exception as e:
        return jsonify({"error": f"Erreur: {str(e)}"}), 500
 
 
@app.route("/api/paiement/verifier", methods=["POST"])
def verifier_paiement():
    """
    Étape 2 — Après redirection WebView, le frontend demande la vérification.
    CinetPay confirme si le paiement est ACCEPTED ou non.
    """
    d              = request.get_json()
    transaction_id = d.get("transaction_id", "").strip()
 
    if not transaction_id:
        return jsonify({"error": "transaction_id requis"}), 400
 
    # Vérifier d'abord en base (si webhook déjà reçu)
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM transactions WHERE transaction_id=%s", (transaction_id,))
        trans = row(cur)
        cur.close(); conn.close()
 
        if trans and trans["statut"] == "paye":
            # Déjà confirmé via webhook → pas besoin d'appeler CinetPay
            return jsonify({"success": True, "paye": True, "statut": "ACCEPTED", "source": "cache"}), 200
 
    except Exception:
        pass  # si erreur DB, on continue avec l'API CinetPay
 
    # Appel API CinetPay pour vérification fraîche
    payload = {
        "apikey":         CINETPAY_API_KEY,
        "site_id":        CINETPAY_SITE_ID,
        "transaction_id": transaction_id,
    }
 
    try:
        resp   = http_req.post(CINETPAY_CHK_URL, json=payload, timeout=15)
        result = resp.json()
        code   = str(result.get("code", ""))
        data   = result.get("data", {})
        statut = data.get("status", "")
 
        paye = (code == "00" and statut == "ACCEPTED")
 
        # Mettre à jour en base
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE transactions
            SET statut = %s, cinetpay_ref = %s, updated_at = NOW()
            WHERE transaction_id = %s
        """, ("paye" if paye else "echec", data.get("payment_method"), transaction_id))
        conn.commit()
        cur.close(); conn.close()
 
        return jsonify({
            "success": True,
            "paye":    paye,
            "statut":  statut,
            "code":    code,
            "moyen":   data.get("payment_method", "—"),
        }), 200
 
    except http_req.exceptions.Timeout:
        return jsonify({"error": "CinetPay ne répond pas (timeout)."}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route("/api/paiement/notification", methods=["POST"])
def notification_cinetpay():
    """
    Webhook CinetPay — appelé automatiquement par CinetPay après paiement.
    CinetPay envoie les données en form-data OU JSON selon la version.
    """
    # CinetPay peut envoyer en form-data ou JSON
    d = request.get_json(silent=True) or request.form.to_dict()
 
    transaction_id = d.get("cpm_trans_id") or d.get("transaction_id", "")
    cpm_result     = d.get("cpm_result", "")
    payment_ref    = d.get("cpm_payid", "")
 
    if not transaction_id:
        return jsonify({"error": "transaction_id manquant"}), 400
 
    paye = (cpm_result == "00")
 
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("""
            UPDATE transactions
            SET statut = %s, cinetpay_ref = %s, updated_at = NOW()
            WHERE transaction_id = %s
        """, ("paye" if paye else "echec", payment_ref, transaction_id))
        conn.commit()
        cur.close(); conn.close()
 
        return jsonify({"status": "ok", "paye": paye}), 200
 
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route("/api/paiement/statut/<transaction_id>", methods=["GET"])
def statut_paiement(transaction_id):
    """Consulter le statut d'une transaction en base (sans appel CinetPay)"""
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM transactions WHERE transaction_id=%s", (transaction_id,))
        trans = row(cur)
        cur.close(); conn.close()
        if not trans:
            return jsonify({"error": "Transaction introuvable"}), 404
        return jsonify({"success": True, "transaction": trans}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
# ═══════════════════════════════════════════════════════════
#  HELPER : créer un colis (partagé par les 3 services)
#  Le colis n'est créé QUE si le paiement est confirmé.
# ═══════════════════════════════════════════════════════════
 
def verifier_transaction_payee(transaction_id: str, telephone: str) -> tuple[bool, str]:
    """
    Vérifie qu'une transaction existe, appartient au bon client et est payée.
    Retourne (ok: bool, message: str)
    """
    if not transaction_id:
        return False, "transaction_id manquant"
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM transactions WHERE transaction_id=%s", (transaction_id,))
        trans = row(cur)
        cur.close(); conn.close()
 
        if not trans:
            return False, "Transaction introuvable"
        if trans["telephone"] != telephone:
            return False, "Transaction non associée à ce client"
        if trans["statut"] != "paye":
            return False, f"Paiement non confirmé (statut: {trans['statut']})"
        if trans["colis_id"] is not None:
            return False, "Cette transaction est déjà utilisée"
 
        return True, "ok"
    except Exception as e:
        return False, str(e)
 
 
def lier_transaction_colis(transaction_id: str, colis_id: int):
    """Lie une transaction à un colis après création."""
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute(
            "UPDATE transactions SET colis_id=%s, updated_at=NOW() WHERE transaction_id=%s",
            (colis_id, transaction_id)
        )
        conn.commit()
        cur.close(); conn.close()
    except Exception:
        pass
 
 
# ═══════════════════════════════════════════════════════════
#  CLIENT — Créer des commandes (paiement requis)
# ═══════════════════════════════════════════════════════════
 
@app.route("/api/colis/recuperer", methods=["POST"])
def recuperer():
    d   = request.get_json()
    tel = d.get("telephone", "").strip()
    tid = d.get("transaction_id", "").strip()
 
    # 1. Vérifier le paiement
    ok, msg = verifier_transaction_payee(tid, tel)
    if not ok:
        return jsonify({"error": f"Paiement invalide : {msg}"}), 402
 
    lieu = d.get("lieu_recuperation", "").strip()
    desc = d.get("description_colis", "").strip()
    adr  = d.get("adresse_client", "").strip()
    dest_id = d.get("adresse_destination_id")
    frais   = float(d.get("frais_calcules", 500))
    note    = d.get("note", "")
 
    if not all([tel, lieu, desc, adr]):
        return jsonify({"error": "Champs obligatoires manquants"}), 400
 
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
 
        cur.execute("SELECT id FROM utilisateurs WHERE telephone=%s AND role='client'", (tel,))
        client = row(cur)
        if not client:
            return jsonify({"error": "Client introuvable"}), 404
 
        code = gen_code()
        cur.execute("""
            INSERT INTO colis (
                code_suivi, type_service, client_id, telephone_client,
                lieu_recuperation, description_colis, adresse_client,
                adresse_destination_id, frais, frais_livraison,
                transaction_id, paiement_statut, note
            ) VALUES (%s,'recuperation',%s,%s,%s,%s,%s,%s,%s,%s,%s,'paye',%s)
            RETURNING *
        """, (code, client["id"], tel, lieu, desc, adr, dest_id, frais, frais, tid, note))
 
        colis = row(cur)
        add_suivi(conn, colis["id"], "en_attente",
                  "Demande de récupération reçue. Paiement confirmé.", "client")
        conn.commit()
        cur.close(); conn.close()
 
        # Lier la transaction au colis
        lier_transaction_colis(tid, colis["id"])
 
        return jsonify({"success": True, "code_suivi": code, "colis": colis}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route("/api/colis/acheter", methods=["POST"])
def acheter():
    d   = request.get_json()
    tel = d.get("telephone", "").strip()
    tid = d.get("transaction_id", "").strip()
 
    ok, msg = verifier_transaction_payee(tid, tel)
    if not ok:
        return jsonify({"error": f"Paiement invalide : {msg}"}), 402
 
    art     = d.get("article", "").strip()
    bou     = d.get("boutique", "").strip()
    bud     = float(d.get("budget_article", 0))
    adr     = d.get("adresse_client", "").strip()
    dest_id = d.get("adresse_destination_id")
    frais       = float(d.get("frais_calcules", 700))
    frais_liv   = float(d.get("frais_livraison", 0))
    frais_serv  = float(d.get("frais_service", 0))
    note        = d.get("note", "")
 
    if not all([tel, art, bou, adr]):
        return jsonify({"error": "Champs obligatoires manquants"}), 400
 
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
 
        cur.execute("SELECT id FROM utilisateurs WHERE telephone=%s AND role='client'", (tel,))
        client = row(cur)
        if not client:
            return jsonify({"error": "Client introuvable"}), 404
 
        code = gen_code()
        cur.execute("""
            INSERT INTO colis (
                code_suivi, type_service, client_id, telephone_client,
                article, boutique, budget_article, adresse_client,
                adresse_destination_id, frais, frais_livraison, frais_service,
                transaction_id, paiement_statut, note
            ) VALUES (%s,'achat',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'paye',%s)
            RETURNING *
        """, (code, client["id"], tel, art, bou, bud, adr,
              dest_id, frais, frais_liv, frais_serv, tid, note))
 
        colis = row(cur)
        add_suivi(conn, colis["id"], "en_attente",
                  f"Commande d'achat reçue : {art}. Paiement confirmé.", "client")
        conn.commit()
        cur.close(); conn.close()
        lier_transaction_colis(tid, colis["id"])
 
        return jsonify({"success": True, "code_suivi": code, "colis": colis}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route("/api/colis/livrer", methods=["POST"])
def livrer():
    d   = request.get_json()
    tel = d.get("telephone", "").strip()
    tid = d.get("transaction_id", "").strip()
 
    ok, msg = verifier_transaction_payee(tid, tel)
    if not ok:
        return jsonify({"error": f"Paiement invalide : {msg}"}), 402
 
    nom      = d.get("nom_destinataire", "").strip()
    teld     = d.get("telephone_dest", "").strip()
    adrd     = d.get("adresse_livraison", "").strip()
    dest_id  = d.get("adresse_destination_id")
    desc     = d.get("description_envoi", "").strip()
    adrc     = d.get("adresse_client", "").strip()
    dep_id   = d.get("adresse_depart_id")
    frais    = float(d.get("frais_calcules", 500))
    note     = d.get("note", "")
 
    if not all([tel, nom, teld, adrd, desc, adrc]):
        return jsonify({"error": "Champs obligatoires manquants"}), 400
 
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
 
        cur.execute("SELECT id FROM utilisateurs WHERE telephone=%s AND role='client'", (tel,))
        client = row(cur)
        if not client:
            return jsonify({"error": "Client introuvable"}), 404
 
        code = gen_code()
        cur.execute("""
            INSERT INTO colis (
                code_suivi, type_service, client_id, telephone_client,
                nom_destinataire, telephone_dest, adresse_livraison,
                adresse_destination_id, description_envoi,
                adresse_client, frais, frais_livraison,
                transaction_id, paiement_statut, note
            ) VALUES (%s,'livraison',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'paye',%s)
            RETURNING *
        """, (code, client["id"], tel, nom, teld, adrd,
              dest_id, desc, adrc, frais, frais, tid, note))
 
        colis = row(cur)
        add_suivi(conn, colis["id"], "en_attente",
                  f"Livraison pour {nom} enregistrée. Paiement confirmé.", "client")
        conn.commit()
        cur.close(); conn.close()
        lier_transaction_colis(tid, colis["id"])
 
        return jsonify({"success": True, "code_suivi": code, "colis": colis}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route("/api/client/<telephone>/historique", methods=["GET"])
def historique_client(telephone):
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.*, u.telephone AS livreur_tel, u.nom AS livreur_nom
            FROM colis c
            LEFT JOIN utilisateurs u ON u.id = c.livreur_id
            WHERE c.telephone_client = %s
            ORDER BY c.created_at DESC
        """, (telephone,))
        data = rows(cur)
        cur.close(); conn.close()
        return jsonify({"success": True, "commandes": data}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
# ═══════════════════════════════════════════════════════════
#  SUIVI
# ═══════════════════════════════════════════════════════════
 
@app.route("/api/colis/suivi/<code>", methods=["GET"])
def suivi(code):
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.*, u.nom AS livreur_nom, u.telephone AS livreur_tel
            FROM colis c
            LEFT JOIN utilisateurs u ON u.id = c.livreur_id
            WHERE c.code_suivi = %s
        """, (code.upper(),))
        colis = row(cur)
        if not colis:
            return jsonify({"error": "Code introuvable"}), 404
 
        cur.execute("""
            SELECT statut, message, auteur_role, created_at
            FROM suivi_statuts WHERE colis_id=%s ORDER BY created_at ASC
        """, (colis["id"],))
        hist = rows(cur)
        cur.close(); conn.close()
        return jsonify({"success": True, "colis": colis, "historique": hist}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
# ═══════════════════════════════════════════════════════════
#  LIVREUR
# ═══════════════════════════════════════════════════════════
 
@app.route("/api/livreur/<int:livreur_id>/commandes", methods=["GET"])
def commandes_livreur(livreur_id):
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM colis WHERE livreur_id=%s ORDER BY created_at DESC", (livreur_id,))
        data = rows(cur)
        cur.close(); conn.close()
        return jsonify({"success": True, "commandes": data}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
STATUTS_LIVREUR = ["en_route_recuperation", "recupere", "en_livraison", "livre"]
 
@app.route("/api/livreur/colis/<code>/statut", methods=["PUT"])
def livreur_update_statut(code):
    d          = request.get_json()
    statut     = d.get("statut", "").strip()
    message    = d.get("message", "")
    livreur_id = d.get("livreur_id")
 
    if statut not in STATUTS_LIVREUR:
        return jsonify({"error": f"Statut invalide. Valeurs: {STATUTS_LIVREUR}"}), 400
 
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            UPDATE colis SET statut=%s, updated_at=NOW()
            WHERE code_suivi=%s AND livreur_id=%s RETURNING *
        """, (statut, code.upper(), livreur_id))
        colis = row(cur)
        if not colis:
            return jsonify({"error": "Colis introuvable ou non assigné"}), 404
        add_suivi(conn, colis["id"], statut, message or f"Statut: {statut}", "livreur")
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "colis": colis}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
# ═══════════════════════════════════════════════════════════
#  ADMIN
# ═══════════════════════════════════════════════════════════
 
@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
 
        cur.execute("SELECT COUNT(*) AS n FROM colis")
        total = row(cur)["n"]
 
        cur.execute("SELECT COUNT(*) AS n FROM colis WHERE statut='en_attente'")
        en_attente = row(cur)["n"]
 
        cur.execute("SELECT COUNT(*) AS n FROM colis WHERE statut='en_livraison'")
        en_livraison = row(cur)["n"]
 
        cur.execute("SELECT COUNT(*) AS n FROM colis WHERE statut='livre'")
        livres = row(cur)["n"]
 
        cur.execute("SELECT COUNT(*) AS n FROM utilisateurs WHERE role='client'")
        clients = row(cur)["n"]
 
        cur.execute("SELECT COUNT(*) AS n FROM utilisateurs WHERE role='livreur' AND actif=TRUE")
        livreurs = row(cur)["n"]
 
        cur.execute("SELECT COALESCE(SUM(frais),0) AS rev FROM colis WHERE statut='livre'")
        revenus = float(row(cur)["rev"])
 
        cur.execute("SELECT COUNT(*) AS n FROM transactions WHERE statut='paye'")
        tx_payees = row(cur)["n"]
 
        cur.close(); conn.close()
 
        return jsonify({"success": True, "stats": {
            "total_commandes":  total,
            "en_attente":       en_attente,
            "en_livraison":     en_livraison,
            "livres":           livres,
            "total_clients":    clients,
            "livreurs_actifs":  livreurs,
            "revenus_fcfa":     revenus,
            "transactions_payees": tx_payees,
        }}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route("/api/admin/commandes", methods=["GET"])
def admin_all_commandes():
    statut = request.args.get("statut")
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if statut:
            cur.execute("""
                SELECT c.*, u.nom AS livreur_nom, u.telephone AS livreur_tel
                FROM colis c LEFT JOIN utilisateurs u ON u.id=c.livreur_id
                WHERE c.statut=%s ORDER BY c.created_at DESC
            """, (statut,))
        else:
            cur.execute("""
                SELECT c.*, u.nom AS livreur_nom, u.telephone AS livreur_tel
                FROM colis c LEFT JOIN utilisateurs u ON u.id=c.livreur_id
                ORDER BY c.created_at DESC
            """)
        data = rows(cur)
        cur.close(); conn.close()
        return jsonify({"success": True, "commandes": data}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route("/api/admin/colis/<code>/assigner", methods=["PUT"])
def assigner_livreur(code):
    d          = request.get_json()
    livreur_id = d.get("livreur_id")
    if not livreur_id:
        return jsonify({"error": "livreur_id requis"}), 400
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            UPDATE colis SET livreur_id=%s, statut='confirme', updated_at=NOW()
            WHERE code_suivi=%s RETURNING *
        """, (livreur_id, code.upper()))
        colis = row(cur)
        if not colis:
            return jsonify({"error": "Colis introuvable"}), 404
        cur.execute("SELECT nom, telephone FROM utilisateurs WHERE id=%s", (livreur_id,))
        liv = row(cur)
        add_suivi(conn, colis["id"], "confirme",
                  f"Assigné au livreur {liv['nom'] if liv else livreur_id}.", "admin")
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "colis": colis}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
TOUS_STATUTS = ["en_attente","confirme","en_route_recuperation",
                "recupere","en_livraison","livre","annule"]
 
@app.route("/api/admin/colis/<code>/statut", methods=["PUT"])
def admin_update_statut(code):
    d       = request.get_json()
    statut  = d.get("statut", "").strip()
    message = d.get("message", "")
    if statut not in TOUS_STATUTS:
        return jsonify({"error": "Statut invalide"}), 400
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            UPDATE colis SET statut=%s, updated_at=NOW()
            WHERE code_suivi=%s RETURNING *
        """, (statut, code.upper()))
        colis = row(cur)
        if not colis:
            return jsonify({"error": "Colis introuvable"}), 404
        add_suivi(conn, colis["id"], statut, message or f"Statut: {statut}", "admin")
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "colis": colis}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route("/api/admin/livreurs", methods=["GET"])
def list_livreurs():
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT u.id, u.nom, u.telephone, u.actif, u.created_at,
                   COUNT(c.id) AS total_livraisons,
                   SUM(CASE WHEN c.statut='livre' THEN 1 ELSE 0 END) AS livrees
            FROM utilisateurs u
            LEFT JOIN colis c ON c.livreur_id = u.id
            WHERE u.role='livreur'
            GROUP BY u.id ORDER BY u.created_at DESC
        """)
        data = rows(cur)
        cur.close(); conn.close()
        return jsonify({"success": True, "livreurs": data}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route("/api/admin/livreurs", methods=["POST"])
def create_livreur():
    d   = request.get_json()
    tel = d.get("telephone", "").strip()
    nom = d.get("nom", "").strip()
    pin = d.get("pin", "").strip()
    if not all([tel, nom, pin]) or len(pin) < 4:
        return jsonify({"error": "Téléphone, nom et PIN (min 4) requis"}), 400
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO utilisateurs (telephone, role, pin_hash, nom)
            VALUES (%s,'livreur',%s,%s)
            RETURNING id, nom, telephone, actif, created_at
        """, (tel, hash_pin(pin), nom))
        user = row(cur)
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "livreur": user}), 201
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Ce numéro existe déjà"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
@app.route("/api/admin/livreurs/<int:lid>/toggle", methods=["PUT"])
def toggle_livreur(lid):
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            UPDATE utilisateurs SET actif = NOT actif
            WHERE id=%s AND role='livreur' RETURNING id, nom, actif
        """, (lid,))
        user = row(cur)
        conn.commit(); cur.close(); conn.close()
        if not user:
            return jsonify({"error": "Livreur introuvable"}), 404
        return jsonify({"success": True, "livreur": user}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500
 
 
# ─────────────────────────────────────────────
#  SANTÉ
# ─────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({
        "status":  "ok",
        "app":     "FABLA",
        "version": "3.0",
        "ville":   "Assinie-Mafia",
    }), 200
 
@app.route("/api/paiement/test", methods=["GET"])
def test_cinetpay():
    """
    Route de diagnostic CinetPay.
    Appel : GET /api/paiement/test
    Affiche ce qui serait envoyé à CinetPay sans vraiment créer de transaction.
    """
    import json as _json
 
    tel_test       = "0700000000"
    tel_intl       = "225" + tel_test[1:]
    transaction_id = gen_transaction_id()
 
    payload_test = {
        "apikey":                  CINETPAY_API_KEY,
        "site_id":                 int(CINETPAY_SITE_ID) if str(CINETPAY_SITE_ID).isdigit() else CINETPAY_SITE_ID,
        "transaction_id":          transaction_id,
        "amount":                  500,
        "currency":                "XOF",
        "description":             "Test paiement FABLA",
        "channels":                "ALL",
        "lang":                    "fr",
        "metadata":                tel_test,
        "return_url":              RETURN_URL,
        "cancel_url":              CANCEL_URL,
        "notify_url":              NOTIFY_URL,
        "customer_name":           "Client",
        "customer_surname":        "FABLA",
        "customer_email":          f"{tel_test}@fabla.ci",
        "customer_phone_number":   tel_intl,
        "customer_address":        "Assinie-Mafia",
        "customer_city":           "Assinie",
        "customer_country":        "CI",
        "customer_state":          "CI",
        "customer_zip":            "00225",
    }
 
    # Vrai appel vers CinetPay
    try:
        resp   = http_req.post(CINETPAY_PAY_URL, json=payload_test, timeout=15)
        result = resp.json()
    except Exception as e:
        result = {"error": str(e)}
 
    return jsonify({
        "payload_envoyé": {k: v for k, v in payload_test.items() if k != "apikey"},
        "api_key_defini":  CINETPAY_API_KEY != "VOTRE_API_KEY_ICI",
        "site_id_defini":  CINETPAY_SITE_ID != "VOTRE_SITE_ID_ICI",
        "reponse_cinetpay": result,
    }), 200
# ─────────────────────────────────────────────
#  LANCEMENT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
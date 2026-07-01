"""
FABLA v3 — Backend complet (Flask + PostgreSQL) avec Genius Pay
Ville     : Assinie-Mafia
Rôles     : client | livreur | admin
Services  : Récupérer / Acheter / Livrer un colis
Paiement  : Automatique via Genius Pay
"""

import os, random, string, hashlib
import psycopg2, psycopg2.extras
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import requests
import hmac
import json
import logging

load_dotenv()
app = Flask(__name__)
CORS(app)

# Configuration des logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG GENIUS PAY
# ─────────────────────────────────────────────
GENIUS_PAY_API_URL = os.getenv("GENIUS_PAY_API_URL", "https://api.geniuspay.com/v1/merchant")
GENIUS_PAY_API_KEY = os.getenv("GENIUS_PAY_API_KEY")
GENIUS_PAY_API_SECRET = os.getenv("GENIUS_PAY_API_SECRET") 
GENIUS_PAY_MERCHANT_ID = os.getenv("GENIUS_PAY_MERCHANT_ID")
GENIUS_PAY_WEBHOOK_SECRET = os.getenv("GENIUS_PAY_WEBHOOK_SECRET")
GENIUS_PAY_CALLBACK_URL = os.getenv("GENIUS_PAY_CALLBACK_URL", "https://fabla-backend-production.up.railway.app/api/payment/webhook")
GENIUS_PAY_REDIRECT_URL = os.getenv("GENIUS_PAY_REDIRECT_URL", "fabla://payment/result")
# ─────────────────────────────────────────────
# CONFIG DB
# ─────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME", "fabla_db"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "password"),
}

logger.info(f"🔗 Connecting to DB: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']} as {DB_CONFIG['user']}")

def get_conn():
    return psycopg2.connect(**DB_CONFIG)

def hash_pwd(pwd):
    return hashlib.sha256(pwd.encode()).hexdigest()

# ─────────────────────────────────────────────
# CALCUL DES FRAIS
# ─────────────────────────────────────────────
TARIF_PAR_METRE = 0.5  # FCFA par mètre
FRAIS_MINIMUM   = 200  # FCFA minimum
COMMISSION_ACHAT = 0.05  # 5% pour le service d'achat

def calculer_frais(distance_metres, type_service, valeur_marchandise=0):
    # Frais de livraison : base sur la distance
    frais_livraison = max(round(float(distance_metres) * TARIF_PAR_METRE), FRAIS_MINIMUM)
    
    # Frais de service : uniquement pour le type 'achat'
    if type_service == 'achat' and valeur_marchandise > 0:
        frais_service = round(float(valeur_marchandise) * COMMISSION_ACHAT)
    else:
        frais_service = 0
    
    return {
        'frais_livraison': frais_livraison,
        'frais_service':   frais_service,
        'total':           frais_livraison + frais_service,
    }

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def gen_code():
    return f"FAB-{''.join(random.choices(string.ascii_uppercase + string.digits, k=6))}"

def gen_code_livreur():
    return f"LIV-{''.join(random.choices(string.digits, k=3))}"

def add_suivi(conn, colis_id, statut, message, auteur_id=None):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO suivi_statuts (colis_id, statut, message, auteur_id) VALUES (%s,%s,%s,%s)",
        (colis_id, statut, message, auteur_id)
    )
    cur.close()

def get_user_by_tel(telephone):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE telephone=%s", (telephone,))
    u = cur.fetchone()
    cur.close()
    conn.close()
    return dict(u) if u else None

def safe_user(u):
    if u:
        u = dict(u)
        u.pop('password_hash', None)
    return u

# ─────────────────────────────────────────────
# GENIUS PAY HELPERS
# ─────────────────────────────────────────────
def genius_pay_headers():
    """Retourne les headers pour les appels API Genius Pay"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-API-Key": GENIUS_PAY_API_KEY,
        "X-API-Secret": GENIUS_PAY_API_SECRET,
    }
    if GENIUS_PAY_MERCHANT_ID:
        headers["X-Merchant-Id"] = GENIUS_PAY_MERCHANT_ID
    return headers

def initiate_genius_pay_payment(amount, description, order_id, customer_phone, customer_name, payment_method=None):
    try:
        payload = {
            "amount": amount,
            "currency": "XOF",
            "description": description,
            "customer": {
                "name": customer_name or "Client FABLA",
                "phone": customer_phone,
                "email": f"{customer_phone}@fabla.com",
            },
            "success_url": GENIUS_PAY_REDIRECT_URL + "?status=success&order_id=" + order_id,
            "error_url": GENIUS_PAY_REDIRECT_URL + "?status=failed&order_id=" + order_id,
            "metadata": {
                "order_id": order_id,
            }
        }
        
        if payment_method:
            payload["payment_method"] = payment_method
        
        url = f"{GENIUS_PAY_API_URL}/payments"
        logger.info(f"🔗 Genius Pay URL: {url}")
        
        response = requests.post(
            url,
            json=payload,
            headers=genius_pay_headers(),
            timeout=30
        )
        
        logger.info(f"📨 Response status: {response.status_code}")
        logger.info(f"📨 Response body: {response.text[:500]}")
        
        if response.status_code in [200, 201]:
            data = response.json()
            return {
                "success": True,
                "payment_id": data.get("data", {}).get("id"),
                "payment_url": data.get("data", {}).get("payment_url") or data.get("data", {}).get("checkout_url"),
                "status": data.get("data", {}).get("status", "pending"),
                "reference": data.get("data", {}).get("reference"),
            }
        else:
            error_data = response.json() if response.text else {}
            return {
                "success": False,
                "error": error_data.get("error", {}).get("message", "Erreur Genius Pay"),
                "status_code": response.status_code,
            }
            
    except Exception as e:
        logger.error(f"❌ Erreur Genius Pay: {str(e)}")
        return {"success": False, "error": str(e)}

def verify_genius_pay_payment(payment_id):
    """Vérifie le statut d'un paiement Genius Pay"""
    try:
        response = requests.get(
            f"{GENIUS_PAY_API_URL}/payments/{payment_id}",
            headers=genius_pay_headers(),
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            return {
                "success": True,
                "payment_id": payment_id,
                "status": data.get("status"),
                "amount": data.get("amount"),
                "customer": data.get("customer"),
                "reference": data.get("reference"),
            }
        else:
            return {
                "success": False,
                "error": "Impossible de vérifier le paiement",
                "status_code": response.status_code,
            }
            
    except Exception as e:
        logger.error(f"❌ Erreur vérification Genius Pay: {e}")
        return {"success": False, "error": str(e)}

# ─────────────────────────────────────────────
# INIT TABLES
# ─────────────────────────────────────────────
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id             SERIAL PRIMARY KEY,
            telephone      VARCHAR(20) UNIQUE NOT NULL,
            role           VARCHAR(10) NOT NULL DEFAULT 'client',
            nom            VARCHAR(100),
            password_hash  VARCHAR(64),
            code_livreur   VARCHAR(10),
            actif          BOOLEAN DEFAULT TRUE,
            email          VARCHAR(100),
            created_at     TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS colis (
            id                     SERIAL PRIMARY KEY,
            code_suivi             VARCHAR(12) UNIQUE NOT NULL,
            type_service           VARCHAR(20) NOT NULL,
            client_id              INTEGER REFERENCES users(id),
            telephone_client       VARCHAR(20) NOT NULL,
            livreur_id             INTEGER REFERENCES users(id),

            -- Récupération
            lieu_recuperation      TEXT,
            description_colis      TEXT,

            -- Achat
            article                TEXT,
            boutique               TEXT,
            budget_article         NUMERIC(10,2),

            -- Livraison
            nom_destinataire       TEXT,
            telephone_dest         VARCHAR(20),
            adresse_livraison      TEXT,
            description_envoi      TEXT,

            -- Commun
            adresse_client         TEXT,
            note_supplementaire    TEXT,

            -- Frais
            frais_livraison        NUMERIC(10,2) DEFAULT 0,
            frais_service          NUMERIC(10,2) DEFAULT 0,
            distance_metres        NUMERIC(10,2) DEFAULT 0,
            adresse_destination_id INTEGER,

            -- Paiement
            operateur_paiement     VARCHAR(20) DEFAULT 'orange',
            paiement_statut        VARCHAR(30) DEFAULT 'en_attente_confirmation',
            payment_id             VARCHAR(100),
            payment_reference      VARCHAR(100),

            -- Statut
            statut                 VARCHAR(30) DEFAULT 'en_attente',
            created_at             TIMESTAMP DEFAULT NOW(),
            updated_at             TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS suivi_statuts (
            id         SERIAL PRIMARY KEY,
            colis_id   INTEGER REFERENCES colis(id),
            statut     VARCHAR(50) NOT NULL,
            message    TEXT,
            auteur_id  INTEGER REFERENCES users(id),
            created_at TIMESTAMP DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id              SERIAL PRIMARY KEY,
            colis_id        INTEGER REFERENCES colis(id),
            payment_id      VARCHAR(100) UNIQUE,
            reference       VARCHAR(100),
            amount          NUMERIC(10,2),
            currency        VARCHAR(3) DEFAULT 'XOF',
            status          VARCHAR(30),
            payment_method  VARCHAR(30),
            customer_phone  VARCHAR(20),
            customer_name   VARCHAR(100),
            metadata        JSONB,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW()
        );
    """)

    # Admin par défaut
    cur.execute("SELECT id FROM users WHERE role='admin' LIMIT 1")
    if not cur.fetchone():
        try:
            cur.execute("""
                INSERT INTO users (telephone, role, nom, password_hash, actif)
                VALUES (%s, 'admin', 'Admin FABLA', %s, TRUE)
            """, ('+2250710069791', hash_pwd('123456')))
            logger.info("✅ Admin créé — tél: +2250710069791 / MDP: 123456")
        except Exception as e:
            logger.warning(f"⚠️ Admin déjà existant: {e}")

    conn.commit()
    cur.close()
    conn.close()
    logger.info("✅ Tables initialisées.")
    migrer_base_donnees()

def migrer_base_donnees():
    """Ajoute les colonnes manquantes sans casser l'existant."""
    migrations = [
        ("frais_service",          "NUMERIC(10,2) DEFAULT 0"),
        ("distance_metres",        "NUMERIC(10,2) DEFAULT 0"),
        ("adresse_destination_id", "INTEGER"),
        ("operateur_paiement",     "VARCHAR(20) DEFAULT 'orange'"),
        ("paiement_statut",        "VARCHAR(30) DEFAULT 'en_attente_confirmation'"),
        ("payment_id",             "VARCHAR(100)"),
        ("payment_reference",      "VARCHAR(100)"),
        ("email",                  "VARCHAR(100)"),
        ("adresse_livraison",      "TEXT"),
    ]
    try:
        conn = get_conn()
        cur = conn.cursor()
        for col_name, col_type in migrations:
            tables = ['colis']
            for table in tables:
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name=%s AND column_name=%s
                """, (table, col_name))
                if not cur.fetchone():
                    try:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_name} {col_type}")
                        logger.info(f"✅ Colonne '{col_name}' ajoutée à {table}")
                    except Exception as e:
                        logger.warning(f"⚠️ Erreur ajout colonne {col_name} à {table}: {e}")
        conn.commit()
        cur.close()
        conn.close()
        logger.info("✅ Migration terminée.")
    except Exception as e:
        logger.warning(f"⚠️ Migration: {e}")

init_db()

# ═══════════════════════════════════════════════
#  AUTH
# ═══════════════════════════════════════════════
@app.route("/api/auth", methods=["POST"])
def auth():
    data = request.get_json()
    telephone = data.get("telephone", "").strip()
    role = data.get("role", "client").strip()
    password = data.get("password", "").strip()
    code_liv = data.get("code_livreur", "").strip()

    if not telephone or len(telephone) < 4:
        return jsonify({"error": "Numéro invalide"}), 400

    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if role == "client":
        cur.execute("SELECT * FROM users WHERE telephone=%s AND role='client'", (telephone,))
        user = cur.fetchone()
        if not user:
            cur.execute(
                "INSERT INTO users (telephone, role) VALUES (%s,'client') RETURNING *",
                (telephone,)
            )
            user = cur.fetchone()
            conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "role": "client", "user": safe_user(user)}), 200

    if role == "livreur":
        if not code_liv:
            return jsonify({"error": "Code livreur requis"}), 400
        cur.execute("""
            SELECT * FROM users
            WHERE telephone=%s AND role='livreur' AND code_livreur=%s AND actif=TRUE
        """, (telephone, code_liv))
        user = cur.fetchone()
        cur.close()
        conn.close()
        if not user:
            return jsonify({"error": "Identifiants livreur incorrects ou compte inactif"}), 401
        return jsonify({"success": True, "role": "livreur", "user": safe_user(user)}), 200

    if role == "admin":
        if not password:
            return jsonify({"error": "Mot de passe requis"}), 400
        cur.execute("""
            SELECT * FROM users
            WHERE telephone=%s AND role='admin' AND password_hash=%s
        """, (telephone, hash_pwd(password)))
        user = cur.fetchone()
        cur.close()
        conn.close()
        if not user:
            return jsonify({"error": "Identifiants admin incorrects"}), 401
        return jsonify({"success": True, "role": "admin", "user": safe_user(user)}), 200

    return jsonify({"error": "Rôle non reconnu"}), 400

# ═══════════════════════════════════════════════
#  CLIENT — 3 services
# ═══════════════════════════════════════════════
@app.route("/api/colis/recuperer", methods=["POST"])
def recuperer_colis():
    data = request.get_json()
    logger.info(f"📦 Récupération - Données reçues: {data}")
    
    telephone = data.get("telephone", "").strip()
    lieu_recuperation = data.get("lieu_recuperation", "").strip()
    description_colis = data.get("description_colis", "").strip()
    adresse_client = data.get("adresse_client", "").strip()
    note = data.get("note_supplementaire", "")
    distance_metres = float(data.get("distance_metres", 0))
    adresse_destination_id = data.get("adresse_destination_id")
    operateur = data.get("operateur_paiement", "genius_pay")

    if not all([telephone, lieu_recuperation, description_colis, adresse_client]):
        missing = []
        if not telephone: missing.append("telephone")
        if not lieu_recuperation: missing.append("lieu_recuperation")
        if not description_colis: missing.append("description_colis")
        if not adresse_client: missing.append("adresse_client")
        logger.error(f"❌ Champs manquants: {missing}")
        return jsonify({"error": f"Champs obligatoires manquants: {missing}"}), 400

    user = get_user_by_tel(telephone)
    if not user:
        return jsonify({"error": "Client introuvable"}), 404

    frais = calculer_frais(distance_metres, "recuperation")

    try:
        code = gen_code()
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO colis (
                code_suivi, type_service, client_id, telephone_client,
                lieu_recuperation, description_colis, adresse_client,
                note_supplementaire, frais_livraison, frais_service,
                distance_metres, adresse_destination_id,
                operateur_paiement, paiement_statut, statut
            ) VALUES (
                %s,'recuperation',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                'en_attente_confirmation','en_attente'
            ) RETURNING *
        """, (code, user["id"], telephone, lieu_recuperation, description_colis,
              adresse_client, note, frais['frais_livraison'], frais['frais_service'],
              distance_metres, adresse_destination_id, operateur))
        colis = dict(cur.fetchone())
        add_suivi(conn, colis["id"], "en_attente",
                  f"Demande reçue. Frais : {frais['total']} FCFA. Paiement {operateur} en attente.",
                  user["id"])
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"✅ Commande créée: {code}")
        return jsonify({"success": True, "code_suivi": code, "colis": colis}), 201
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/colis/acheter", methods=["POST"])
def acheter_colis():
    data = request.get_json()
    logger.info(f"📦 Achat - Données reçues: {data}")
    
    telephone = data.get("telephone", "").strip()
    article = data.get("article", "").strip()
    boutique = data.get("boutique", "").strip()
    budget_article = float(data.get("budget_article", 0))
    adresse_client = data.get("adresse_client", "").strip()
    note = data.get("note_supplementaire", "")
    distance_metres = float(data.get("distance_metres", 0))
    adresse_destination_id = data.get("adresse_destination_id")
    operateur = data.get("operateur_paiement", "genius_pay")

    if not all([telephone, article, boutique, adresse_client]):
        missing = []
        if not telephone: missing.append("telephone")
        if not article: missing.append("article")
        if not boutique: missing.append("boutique")
        if not adresse_client: missing.append("adresse_client")
        logger.error(f"❌ Champs manquants: {missing}")
        return jsonify({"error": f"Champs obligatoires manquants: {missing}"}), 400

    user = get_user_by_tel(telephone)
    if not user:
        return jsonify({"error": "Client introuvable"}), 404

    frais = calculer_frais(distance_metres, "achat", budget_article)

    try:
        code = gen_code()
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO colis (
                code_suivi, type_service, client_id, telephone_client,
                article, boutique, budget_article, adresse_client,
                note_supplementaire, frais_livraison, frais_service,
                distance_metres, adresse_destination_id,
                operateur_paiement, paiement_statut, statut
            ) VALUES (
                %s,'achat',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                'en_attente_confirmation','en_attente'
            ) RETURNING *
        """, (code, user["id"], telephone, article, boutique, budget_article,
              adresse_client, note, frais['frais_livraison'], frais['frais_service'],
              distance_metres, adresse_destination_id, operateur))
        colis = dict(cur.fetchone())
        add_suivi(conn, colis["id"], "en_attente",
                  f"Commande '{article}'. Livraison: {frais['frais_livraison']} FCFA + Service: {frais['frais_service']} FCFA = {frais['total']} FCFA. Paiement {operateur} en attente.",
                  user["id"])
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"✅ Commande créée: {code}")
        return jsonify({"success": True, "code_suivi": code, "colis": colis}), 201
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/colis/livrer", methods=["POST"])
def livrer_colis():
    data = request.get_json()
    logger.info(f"📦 Livraison - Données reçues: {data}")
    
    telephone = data.get("telephone", "").strip()
    nom_destinataire = data.get("nom_destinataire", "").strip()
    telephone_dest = data.get("telephone_dest", "").strip()
    adresse_livraison = data.get("adresse_livraison", "").strip()
    description_envoi = data.get("description_envoi", "").strip()
    adresse_client = data.get("adresse_client", "").strip()
    note = data.get("note_supplementaire", "")
    distance_metres = float(data.get("distance_metres", 0))
    adresse_destination_id = data.get("adresse_destination_id")
    operateur = data.get("operateur_paiement", "genius_pay")

    # ✅ Vérification des champs obligatoires
    required_fields = {
        "telephone": telephone,
        "nom_destinataire": nom_destinataire,
        "telephone_dest": telephone_dest,
        "adresse_livraison": adresse_livraison,
        "description_envoi": description_envoi,
        "adresse_client": adresse_client,
    }
    
    missing = [key for key, value in required_fields.items() if not value]
    if missing:
        logger.error(f"❌ Champs manquants: {missing}")
        return jsonify({"error": f"Champs obligatoires manquants: {missing}"}), 400

    user = get_user_by_tel(telephone)
    if not user:
        return jsonify({"error": "Client introuvable"}), 404

    frais = calculer_frais(distance_metres, "livraison")

    try:
        code = gen_code()
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO colis (
                code_suivi, type_service, client_id, telephone_client,
                nom_destinataire, telephone_dest, adresse_livraison,
                description_envoi, adresse_client, note_supplementaire,
                frais_livraison, frais_service, distance_metres,
                adresse_destination_id, operateur_paiement, paiement_statut, statut
            ) VALUES (
                %s,'livraison',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                'en_attente_confirmation','en_attente'
            ) RETURNING *
        """, (code, user["id"], telephone, nom_destinataire, telephone_dest,
              adresse_livraison, description_envoi, adresse_client, note,
              frais['frais_livraison'], frais['frais_service'],
              distance_metres, adresse_destination_id, operateur))
        colis = dict(cur.fetchone())
        add_suivi(conn, colis["id"], "en_attente",
                  f"Livraison à {nom_destinataire}. Frais : {frais['total']} FCFA. Paiement {operateur} en attente.",
                  user["id"])
        conn.commit()
        cur.close()
        conn.close()
        logger.info(f"✅ Commande créée: {code}")
        return jsonify({"success": True, "code_suivi": code, "colis": colis}), 201
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/colis/suivi/<code>", methods=["GET"])
def suivi_colis(code):
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM colis WHERE code_suivi=%s", (code.upper(),))
        colis = cur.fetchone()
        if not colis:
            return jsonify({"error": "Code introuvable"}), 404
        cur.execute("""
            SELECT s.statut, s.message, s.created_at,
                   u.nom AS auteur_nom, u.role AS auteur_role
            FROM suivi_statuts s
            LEFT JOIN users u ON u.id = s.auteur_id
            WHERE s.colis_id=%s ORDER BY s.created_at ASC
        """, (colis["id"],))
        historique = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({
            "success": True,
            "colis": dict(colis),
            "historique": [dict(h) for h in historique],
        }), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/client/<telephone>/historique", methods=["GET"])
def historique_client(telephone):
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.*, u.nom AS livreur_nom, u.telephone AS livreur_tel
            FROM colis c
            LEFT JOIN users u ON u.id = c.livreur_id
            WHERE c.telephone_client=%s ORDER BY c.created_at DESC
        """, (telephone,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"success": True, "commandes": [dict(r) for r in rows]}), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

# ═══════════════════════════════════════════════
#  LIVREUR
# ═══════════════════════════════════════════════
@app.route("/api/livreur/<int:livreur_id>/missions", methods=["GET"])
def missions_livreur(livreur_id):
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.*, u.telephone AS client_tel, u.nom AS client_nom
            FROM colis c
            JOIN users u ON u.id = c.client_id
            WHERE c.livreur_id=%s ORDER BY c.created_at DESC
        """, (livreur_id,))
        missions = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"success": True, "missions": [dict(m) for m in missions]}), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

STATUTS_LIVREUR = ["en_route_recuperation", "recupere", "en_livraison", "livre"]

@app.route("/api/livreur/colis/<code>/statut", methods=["PUT"])
def livreur_update_statut(code):
    data = request.get_json()
    statut = data.get("statut", "").strip()
    message = data.get("message", "")
    livreur_id = data.get("livreur_id")

    if statut not in STATUTS_LIVREUR:
        return jsonify({"error": "Statut non autorisé pour un livreur"}), 400

    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT * FROM colis WHERE code_suivi=%s AND livreur_id=%s",
            (code.upper(), livreur_id)
        )
        colis = cur.fetchone()
        if not colis:
            return jsonify({"error": "Colis introuvable ou non assigné à ce livreur"}), 403
        cur.execute(
            "UPDATE colis SET statut=%s, updated_at=NOW() WHERE id=%s RETURNING *",
            (statut, colis["id"])
        )
        updated = dict(cur.fetchone())
        add_suivi(conn, colis["id"], statut,
                  message or f"Statut: {statut}", livreur_id)
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "colis": updated}), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/livreur/<int:livreur_id>/stats", methods=["GET"])
def get_livreur_stats(livreur_id):
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(frais_livraison + frais_service), 0) AS total_frais
            FROM colis WHERE livreur_id=%s AND statut='livre'
        """, (livreur_id,))
        result = dict(cur.fetchone())

        cur.execute("""
            SELECT COUNT(*) AS encours FROM colis
            WHERE livreur_id=%s AND statut NOT IN ('livre','annule')
        """, (livreur_id,))
        encours = cur.fetchone()["encours"]

        cur.execute("""
            SELECT COUNT(*) AS nb,
                   COALESCE(SUM(frais_livraison + frais_service), 0) AS frais
            FROM colis
            WHERE livreur_id=%s AND statut='livre' AND DATE(updated_at)=CURRENT_DATE
        """, (livreur_id,))
        today = dict(cur.fetchone())

        cur.close()
        conn.close()

        total_frais = float(result["total_frais"])
        frais_aujourdhui = float(today["frais"])

        return jsonify({
            "success": True,
            "stats": {
                "total_livraisons": result["total"],
                "gains_totaux": round(total_frais * 0.6),
                "encours": encours,
                "livraisons_aujourdhui": today["nb"],
                "gains_aujourdhui": round(frais_aujourdhui * 0.6),
            }
        }), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

# ═══════════════════════════════════════════════
#  ADMIN
# ═══════════════════════════════════════════════
@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        def count(q, p=()):
            cur.execute(q, p)
            return cur.fetchone()

        total = count("SELECT COUNT(*) AS n FROM colis")["n"]
        en_attente = count("SELECT COUNT(*) AS n FROM colis WHERE statut='en_attente'")["n"]
        a_confirmer = count("SELECT COUNT(*) AS n FROM colis WHERE paiement_statut='en_attente_confirmation'")["n"]
        en_livraison = count("SELECT COUNT(*) AS n FROM colis WHERE statut='en_livraison'")["n"]
        livres = count("SELECT COUNT(*) AS n FROM colis WHERE statut='livre'")["n"]
        nb_clients = count("SELECT COUNT(*) AS n FROM users WHERE role='client'")["n"]
        nb_livreurs = count("SELECT COUNT(*) AS n FROM users WHERE role='livreur' AND actif=TRUE")["n"]

        cur.execute("SELECT COALESCE(SUM(frais_livraison + frais_service), 0) AS rev FROM colis WHERE statut='livre'")
        revenus = float(cur.fetchone()["rev"])

        cur.execute("SELECT type_service, COUNT(*) AS nb FROM colis GROUP BY type_service")
        par_type = [dict(r) for r in cur.fetchall()]

        cur.close()
        conn.close()
        return jsonify({
            "success": True,
            "stats": {
                "total_colis": total,
                "en_attente": en_attente,
                "a_confirmer": a_confirmer,
                "en_livraison": en_livraison,
                "livres": livres,
                "nb_clients": nb_clients,
                "nb_livreurs": nb_livreurs,
                "revenus_fcfa": revenus,
                "par_type": par_type,
            }
        }), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/commandes", methods=["GET"])
@app.route("/api/admin/colis", methods=["GET"])
def admin_all_colis():
    statut = request.args.get("statut")
    paiement_statut = request.args.get("paiement_statut")
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        base = """
            SELECT c.*, u.telephone AS client_tel,
                   l.nom AS livreur_nom, l.telephone AS livreur_tel
            FROM colis c
            JOIN users u ON u.id = c.client_id
            LEFT JOIN users l ON l.id = c.livreur_id
        """
        if statut and paiement_statut:
            cur.execute(base + "WHERE c.statut=%s AND c.paiement_statut=%s ORDER BY c.created_at DESC",
                        (statut, paiement_statut))
        elif statut:
            cur.execute(base + "WHERE c.statut=%s ORDER BY c.created_at DESC", (statut,))
        elif paiement_statut:
            cur.execute(base + "WHERE c.paiement_statut=%s ORDER BY c.created_at DESC", (paiement_statut,))
        else:
            cur.execute(base + "ORDER BY c.created_at DESC")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"success": True, "commandes": [dict(r) for r in rows]}), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/colis/<code>/confirmer-paiement", methods=["PUT"])
def confirmer_paiement(code):
    data = request.get_json() or {}
    livreur_id = data.get("livreur_id")
    admin_id = data.get("admin_id")

    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        if livreur_id:
            cur.execute("""
                UPDATE colis
                SET paiement_statut='confirme', statut='confirme',
                    livreur_id=%s, updated_at=NOW()
                WHERE code_suivi=%s RETURNING *
            """, (livreur_id, code.upper()))
        else:
            cur.execute("""
                UPDATE colis
                SET paiement_statut='confirme', updated_at=NOW()
                WHERE code_suivi=%s RETURNING *
            """, (code.upper(),))

        colis = cur.fetchone()
        if not colis:
            return jsonify({"error": "Colis introuvable"}), 404

        msg = "Paiement confirmé par l'admin."
        if livreur_id:
            cur.execute("SELECT nom FROM users WHERE id=%s", (livreur_id,))
            liv = cur.fetchone()
            msg += f" Assigné à {liv['nom'] if liv else livreur_id}."

        add_suivi(conn, colis["id"], "confirme", msg, admin_id)
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "colis": dict(colis)}), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/colis/<code>/rejeter-paiement", methods=["PUT"])
def rejeter_paiement(code):
    data = request.get_json() or {}
    admin_id = data.get("admin_id")
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            UPDATE colis SET paiement_statut='rejete', statut='annule', updated_at=NOW()
            WHERE code_suivi=%s RETURNING *
        """, (code.upper(),))
        colis = cur.fetchone()
        if not colis:
            return jsonify({"error": "Colis introuvable"}), 404
        add_suivi(conn, colis["id"], "annule",
                  "Paiement non reçu. Commande annulée par l'admin.", admin_id)
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "colis": dict(colis)}), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/colis/<code>/assigner", methods=["PUT"])
def assigner_livreur(code):
    data = request.get_json()
    livreur_id = data.get("livreur_id")
    admin_id = data.get("admin_id")
    if not livreur_id:
        return jsonify({"error": "livreur_id requis"}), 400
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            UPDATE colis SET livreur_id=%s, statut='confirme', updated_at=NOW()
            WHERE code_suivi=%s RETURNING *
        """, (livreur_id, code.upper()))
        colis = cur.fetchone()
        if not colis:
            return jsonify({"error": "Colis introuvable"}), 404
        cur.execute("SELECT nom FROM users WHERE id=%s", (livreur_id,))
        liv = cur.fetchone()
        add_suivi(conn, colis["id"], "confirme",
                  f"Livreur assigné : {liv['nom'] if liv else livreur_id}.", admin_id)
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "colis": dict(colis)}), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

STATUTS_VALIDES = [
    "en_attente", "confirme", "en_route_recuperation",
    "recupere", "en_livraison", "livre", "annule"
]

@app.route("/api/admin/colis/<code>/statut", methods=["PUT"])
def admin_update_statut(code):
    data = request.get_json()
    statut = data.get("statut", "").strip()
    message = data.get("message", "")
    admin_id = data.get("admin_id")
    if statut not in STATUTS_VALIDES:
        return jsonify({"error": "Statut invalide"}), 400
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            UPDATE colis SET statut=%s, updated_at=NOW()
            WHERE code_suivi=%s RETURNING *
        """, (statut, code.upper()))
        colis = cur.fetchone()
        if not colis:
            return jsonify({"error": "Colis introuvable"}), 404
        add_suivi(conn, colis["id"], statut,
                  message or f"Admin: statut → {statut}", admin_id)
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "colis": dict(colis)}), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/livreurs", methods=["GET"])
def liste_livreurs():
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT u.id, u.telephone, u.nom, u.code_livreur, u.actif,
                   COUNT(c.id) AS missions_total,
                   SUM(CASE WHEN c.statut='livre' THEN 1 ELSE 0 END) AS livraisons_ok
            FROM users u
            LEFT JOIN colis c ON c.livreur_id = u.id
            WHERE u.role='livreur'
            GROUP BY u.id ORDER BY u.created_at DESC
        """)
        livreurs = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"success": True, "livreurs": [dict(l) for l in livreurs]}), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/livreurs", methods=["POST"])
def creer_livreur():
    data = request.get_json()
    telephone = data.get("telephone", "").strip()
    nom = data.get("nom", "").strip()
    pin = data.get("pin", "").strip()

    if not all([telephone, nom, pin]) or len(pin) < 4:
        return jsonify({"error": "Téléphone, nom et PIN (≥4 caractères) requis"}), 400

    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO users (telephone, role, nom, code_livreur, actif)
            VALUES (%s,'livreur',%s,%s,TRUE) RETURNING id, telephone, nom, code_livreur, actif
        """, (telephone, nom, pin))
        livreur = dict(cur.fetchone())
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({
            "success": True,
            "livreur": livreur,
            "code_livreur": pin,
            "message": f"Livreur créé. Code d'accès : {pin}",
        }), 201
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Ce numéro existe déjà"}), 409
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/livreurs/<int:livreur_id>/toggle", methods=["PUT"])
@app.route("/api/admin/livreurs/<int:livreur_id>/actif", methods=["PUT"])
def toggle_livreur(livreur_id):
    data = request.get_json() or {}
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT actif FROM users WHERE id=%s AND role='livreur'", (livreur_id,))
        livreur = cur.fetchone()
        if not livreur:
            return jsonify({"error": "Livreur introuvable"}), 404
        nouvel_etat = data.get("actif", not livreur["actif"])
        cur.execute(
            "UPDATE users SET actif=%s WHERE id=%s RETURNING id, nom, actif",
            (nouvel_etat, livreur_id)
        )
        result = dict(cur.fetchone())
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "livreur": result}), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/admin/clients", methods=["GET"])
def liste_clients():
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT u.id, u.telephone, u.created_at, COUNT(c.id) AS nb_commandes
            FROM users u
            LEFT JOIN colis c ON c.client_id = u.id
            WHERE u.role='client'
            GROUP BY u.id ORDER BY u.created_at DESC
        """)
        clients = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"success": True, "clients": [dict(c) for c in clients]}), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────
# GENIUS PAY ENDPOINTS
# ─────────────────────────────────────────────
@app.route("/api/payment/initiate", methods=["POST"])
def initiate_payment():
    """Initie un paiement via Genius Pay"""
    data = request.get_json()
    logger.info(f"📦 Initiation paiement - Données reçues: {data}")
    
    order_id = data.get("order_id")
    amount = data.get("amount")
    description = data.get("description", f"Commande {order_id}")
    customer_phone = data.get("customer_phone")
    customer_name = data.get("customer_name", "Client FABLA")
    payment_method = data.get("payment_method")
    
    if not all([order_id, amount, customer_phone]):
        return jsonify({"error": "order_id, amount et customer_phone sont requis"}), 400
    
    # Vérifier que la commande existe
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM colis WHERE code_suivi=%s", (order_id,))
    order = cur.fetchone()
    cur.close()
    conn.close()
    
    if not order:
        return jsonify({"error": "Commande introuvable"}), 404
    
    # Initier le paiement
    payment_result = initiate_genius_pay_payment(
        amount=amount,
        description=description,
        order_id=order_id,
        customer_phone=customer_phone,
        customer_name=customer_name,
        payment_method=payment_method,
    )
    
    if payment_result["success"]:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE colis 
            SET paiement_statut='en_attente_confirmation', 
                operateur_paiement='genius_pay',
                payment_id=%s,
                payment_reference=%s
            WHERE code_suivi=%s
        """, (payment_result["payment_id"], payment_result.get("reference"), order_id))
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({
            "success": True,
            "payment_id": payment_result["payment_id"],
            "payment_url": payment_result["payment_url"],
            "status": payment_result["status"],
            "reference": payment_result.get("reference"),
        }), 200
    else:
        return jsonify({
            "success": False,
            "error": payment_result.get("error", "Erreur lors de l'initiation du paiement"),
        }), 500

@app.route("/api/payment/webhook", methods=["POST"])
def payment_webhook():
    raw_body = request.get_data(as_text=True)
    logger.info(f"📨 Webhook reçu! Body: {raw_body[:500]}")
    
    # Headers attendus par GeniusPay
    signature = request.headers.get('x-webhook-signature', '')
    timestamp = request.headers.get('x-webhook-timestamp', '')
    event_type = request.headers.get('x-webhook-event', '')
    
    logger.info(f"🔑 Signature reçue: {signature}")
    logger.info(f"⏰ Timestamp reçu: {timestamp}")
    logger.info(f"📌 Event reçu: {event_type}")
    
   # if GENIUS_PAY_WEBHOOK_SECRET:
   #     logger.info(f"🔑 Secret utilisé: {GENIUS_PAY_WEBHOOK_SECRET[:10]}...")
        
     #   if signature and timestamp:
            # ✅ Format: timestamp + "." + raw_body (comme dans la doc)
     #       data = f"{timestamp}.{raw_body}"
      #      expected = hmac.new(
       #         GENIUS_PAY_WEBHOOK_SECRET.encode('utf-8'),
       #         data.encode('utf-8'),
       #         hashlib.sha256
        ##    ).hexdigest()
            
         #   logger.info(f"🔑 Signature calculée: {expected}")
         #   logger.info(f"🔑 Signature reçue: {signature}")
          #  logger.info(f"🔑 Correspondent: {signature == expected}")
            
          #  if not hmac.compare_digest(signature, expected):
          #      logger.warning("⚠️ Signature webhook invalide")
           #     return jsonify({
            #        "error": "Unauthorized",
             #       "debug": {
              #          "received": signature,
              #          "expected": expected
                #    }
              #  }), 401
    
    try:
        event_data = json.loads(raw_body)
    except json.JSONDecodeError:
        logger.error("❌ JSON invalide")
        return jsonify({"error": "Invalid JSON"}), 400
    
    event = event_data.get("event")
    data = event_data.get("data", {})
    
    payment_id = data.get("id")
    reference = data.get("reference")
    status = data.get("status")
    order_id = data.get("metadata", {}).get("order_id")
    
    if not payment_id or not status:
        return jsonify({"error": "Données manquantes"}), 400
    
    logger.info(f"📨 Webhook: event={event}, status={status}, order_id={order_id}")
    
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    
    if event == "payment.success" or status == "completed":
        if order_id:
            cur.execute("SELECT * FROM colis WHERE code_suivi=%s", (order_id,))
        else:
            cur.execute("SELECT * FROM colis WHERE payment_id=%s", (payment_id,))
        
        order = cur.fetchone()
        
        if order:
            cur.execute("""
                UPDATE colis 
                SET paiement_statut='confirme', 
                    statut='confirme',
                    updated_at=NOW() 
                WHERE id=%s RETURNING *
            """, (order["id"],))
            updated_order = cur.fetchone()
            
            add_suivi(conn, order["id"], "confirme", 
                     f"✅ Paiement confirmé via Genius Pay (Réf: {reference})", 
                     None)
            
            cur.execute("""
                INSERT INTO payments (colis_id, payment_id, reference, amount, currency, status, payment_method, customer_phone, customer_name, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                order["id"],
                payment_id,
                reference,
                data.get("amount"),
                data.get("currency", "XOF"),
                status,
                data.get("payment_method"),
                data.get("customer", {}).get("phone"),
                data.get("customer", {}).get("name"),
                json.dumps(data)
            ))
            logger.info(f"✅ Commande {order_id} confirmée")
    
    elif event == "payment.failed" or status == "failed":
        if order_id:
            cur.execute("UPDATE colis SET paiement_statut='rejete', statut='annule', updated_at=NOW() WHERE code_suivi=%s RETURNING *", (order_id,))
        else:
            cur.execute("UPDATE colis SET paiement_statut='rejete', statut='annule', updated_at=NOW() WHERE payment_id=%s RETURNING *", (payment_id,))
        order = cur.fetchone()
        
        if order:
            add_suivi(conn, order["id"], "annule", 
                     f"❌ Paiement échoué via Genius Pay (Réf: {reference})", 
                     None)
            logger.info(f"❌ Commande {order_id} annulée")
    
    conn.commit()
    cur.close()
    conn.close()
    
    return jsonify({"success": True, "received": True}), 200


@app.route("/api/payment/verify/<payment_id>", methods=["GET"])
def verify_payment(payment_id):
    """Vérifie le statut d'un paiement"""
    result = verify_genius_pay_payment(payment_id)
    
    if result["success"]:
        return jsonify({
            "success": True,
            "payment_id": result["payment_id"],
            "status": result["status"],
            "amount": result.get("amount"),
            "reference": result.get("reference"),
        }), 200
    else:
        return jsonify({
            "success": False,
            "error": result.get("error", "Impossible de vérifier le paiement"),
        }), 500

@app.route("/api/payment/redirect", methods=["GET"])
def payment_redirect():
    """Page de redirection après paiement"""
    status = request.args.get("status", "pending")
    order_id = request.args.get("order_id")
    reference = request.args.get("reference")
    
    if status == "success" or status == "completed":
        return jsonify({
            "status": "success",
            "message": "Paiement effectué avec succès",
            "order_id": order_id,
            "reference": reference,
        }), 200
    elif status == "failed":
        return jsonify({
            "status": "failed",
            "message": "Le paiement a échoué",
            "order_id": order_id,
            "reference": reference,
        }), 200
    else:
        return jsonify({
            "status": "pending",
            "message": "Paiement en cours de traitement",
            "order_id": order_id,
            "reference": reference,
        }), 200

@app.route("/api/payment/methods", methods=["GET"])
def get_payment_methods():
    """Récupère les méthodes de paiement disponibles"""
    try:
        response = requests.get(
            f"{GENIUS_PAY_API_URL}/payment-methods",
            headers=genius_pay_headers(),
            timeout=30
        )
        
        if response.status_code == 200:
            return jsonify({
                "success": True,
                "methods": response.json().get("methods", []),
            }), 200
        else:
            return jsonify({
                "success": True,
                "methods": [
                    {"id": "wave", "name": "Wave", "icon": "wave"},
                    {"id": "orange_money", "name": "Orange Money", "icon": "orange"},
                ],
            }), 200
    except Exception as e:
        logger.warning(f"⚠️ Erreur récupération méthodes: {e}")
        return jsonify({
            "success": True,
            "methods": [
                {"id": "wave", "name": "Wave", "icon": "wave"},
                {"id": "orange_money", "name": "Orange Money", "icon": "orange"},
            ],
        }), 200

@app.route("/api/payment/status/<order_id>", methods=["GET"])
def get_payment_status(order_id):
    """Récupère le statut du paiement d'une commande"""
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT paiement_statut, payment_id, payment_reference, statut FROM colis WHERE code_suivi=%s",
            (order_id,)
        )
        colis = cur.fetchone()
        cur.close()
        conn.close()
        
        if not colis:
            return jsonify({"error": "Commande introuvable"}), 404
        
        return jsonify({
            "success": True,
            "paiement_statut": colis["paiement_statut"],
            "payment_id": colis["payment_id"],
            "payment_reference": colis["payment_reference"],
            "statut": colis["statut"],
        }), 200
    except Exception as e:
        logger.error(f"❌ Erreur: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/payment/redirect", methods=["GET"])
def payment_redirect():
    """Redirige vers l'historique des commandes du client"""
    status = request.args.get("status", "pending")
    order_id = request.args.get("order_id", "")
    reference = request.args.get("reference", "")
    telephone = request.args.get("telephone", "")
    
    # ✅ Construire l'URL de redirection vers l'historique
    # Utiliser le schéma personnalisé pour l'app React Native
    redirect_url = f"fabla://history?status={status}&order_id={order_id}&reference={reference}"
    
    # ✅ Si le téléphone est fourni, l'ajouter
    if telephone:
        redirect_url += f"&telephone={telephone}"
    
    # ✅ HTML de redirection automatique
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta http-equiv="refresh" content="1;url={redirect_url}">
        <title>Redirection vers FABLA</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: #1A2A4A;
                color: white;
            }}
            .container {{
                text-align: center;
                padding: 40px;
                max-width: 400px;
            }}
            .spinner {{
                border: 4px solid rgba(255,255,255,0.1);
                border-top: 4px solid #4A90D9;
                border-radius: 50%;
                width: 50px;
                height: 50px;
                animation: spin 1s linear infinite;
                margin: 20px auto;
            }}
            @keyframes spin {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
            .button {{
                display: inline-block;
                margin-top: 20px;
                padding: 12px 30px;
                background: #4A90D9;
                color: white;
                text-decoration: none;
                border-radius: 8px;
                border: none;
                font-size: 16px;
                cursor: pointer;
            }}
            .button:hover {{
                background: #2C4A7C;
            }}
            .status-text {{
                font-size: 24px;
                margin-bottom: 10px;
            }}
            .success {{ color: #27ae60; }}
            .failed {{ color: #e74c3c; }}
            .pending {{ color: #f39c12; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="status-text {status}">
                {('✅ Paiement réussi !' if status == 'success' or status == 'completed' else 
                  '❌ Paiement échoué' if status == 'failed' else 
                  '⏳ Paiement en cours')}
            </div>
            <div class="spinner"></div>
            <p>Commande #<strong>{order_id}</strong></p>
            <p>Référence: {reference}</p>
            <p style="margin-top: 10px; font-size: 14px; opacity: 0.7;">
                Vous allez être redirigé vers votre historique de commandes.
            </p>
            <button class="button" onclick="redirectToApp()">
                📱 Voir mes commandes
            </button>
        </div>

        <script>
            function redirectToApp() {{
                const params = new URLSearchParams(window.location.search);
                const status = params.get('status') || 'pending';
                const orderId = params.get('order_id') || '';
                const reference = params.get('reference') || '';
                const telephone = params.get('telephone') || '';
                
                let url = `fabla://history?status=${{status}}&order_id=${{orderId}}&reference=${{reference}}`;
                if (telephone) {{
                    url += `&telephone=${{telephone}}`;
                }}
                window.location.href = url;
            }}

            // ✅ Redirection automatique après 2 secondes
            setTimeout(redirectToApp, 2000);
        </script>
    </body>
    </html>
    '''
    
    return html, 200
# ─────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "app": "FABLA v3",
        "ville": "Assinie-Mafia",
        "genius_pay": "configured" if GENIUS_PAY_API_KEY else "not configured",
        "frais": f"{TARIF_PAR_METRE} FCFA/m | min {FRAIS_MINIMUM} FCFA | achat +10%",
    }), 200

# ─────────────────────────────────────────────
# LANCEMENT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
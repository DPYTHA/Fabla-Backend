"""
FABLA v2 — Backend complet (Flask + PostgreSQL)
Ville     : Assinie-Mafia
Rôles     : client | livreur | admin
Services  : Récupérer / Acheter / Livrer un colis
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2, psycopg2.extras
import os, random, string, hashlib
from datetime import datetime

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────
# CONFIG DB
# ─────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
    "dbname":   os.getenv("DB_NAME",     "fabla_db"),
    "user":     os.getenv("DB_USER",     "postgres"),
    "password": os.getenv("DB_PASSWORD", "password"),
}

def get_conn():
    return psycopg2.connect(**DB_CONFIG)

def hash_pwd(pwd):
    return hashlib.sha256(pwd.encode()).hexdigest()

# ─────────────────────────────────────────────
# INIT TABLES
# ─────────────────────────────────────────────
def init_db():
    conn = get_conn()
    cur  = conn.cursor()

    # Table utilisateurs (3 rôles)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          SERIAL PRIMARY KEY,
            telephone   VARCHAR(20) UNIQUE NOT NULL,
            role        VARCHAR(10) NOT NULL DEFAULT 'client',  -- client | livreur | admin
            nom         VARCHAR(100),
            password_hash VARCHAR(64),          -- NULL pour les clients
            code_livreur  VARCHAR(10),          -- code secret du livreur (ex: LIV-001)
            actif       BOOLEAN DEFAULT TRUE,
            created_at  TIMESTAMP DEFAULT NOW()
        );
    """)

    # Table colis
    cur.execute("""
        CREATE TABLE IF NOT EXISTS colis (
            id                  SERIAL PRIMARY KEY,
            code_suivi          VARCHAR(12) UNIQUE NOT NULL,
            type_service        VARCHAR(20) NOT NULL,
            client_id           INTEGER REFERENCES users(id),
            telephone_client    VARCHAR(20) NOT NULL,
            livreur_id          INTEGER REFERENCES users(id),

            lieu_recuperation   TEXT,
            description_colis   TEXT,

            article             TEXT,
            boutique            TEXT,
            budget_article      NUMERIC(10,2),

            nom_destinataire    TEXT,
            telephone_dest      VARCHAR(20),
            adresse_livraison   TEXT,
            description_envoi   TEXT,

            adresse_client      TEXT,
            note_supplementaire TEXT,
            statut              VARCHAR(30) DEFAULT 'en_attente',
            frais_livraison     NUMERIC(10,2) DEFAULT 0,
            created_at          TIMESTAMP DEFAULT NOW(),
            updated_at          TIMESTAMP DEFAULT NOW()
        );
    """)

    # Historique statuts
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

        # Dans init_db(), après la création des tables
    # Créer un admin par défaut de façon plus robuste
    cur.execute("SELECT id FROM users WHERE role='admin' LIMIT 1")
    if not cur.fetchone():
        admin_phone = '+2250710069791'
        admin_password = '1234'
        admin_password_hash = hash_pwd(admin_password)
        try:
            cur.execute("""
                INSERT INTO users (telephone, role, nom, password_hash, actif)
                VALUES (%s, 'admin', 'Admin FABLA', %s, TRUE)
            """, (admin_phone, admin_password_hash))
            print(f"✅ Admin créé avec succès. Tél: {admin_phone}, MDP: {admin_password}")
        except Exception as e:
            print(f"⚠️ Erreur lors de la création de l'admin: {e}")
        conn.commit()

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def gen_code():
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"FAB-{suffix}"

def gen_code_livreur():
    n = ''.join(random.choices(string.digits, k=3))
    return f"LIV-{n}"

def add_suivi(conn, colis_id, statut, message, auteur_id=None):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO suivi_statuts (colis_id, statut, message, auteur_id) VALUES (%s,%s,%s,%s)",
        (colis_id, statut, message, auteur_id)
    )
    cur.close()

def get_user_by_tel(telephone):
    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE telephone=%s", (telephone,))
    u = cur.fetchone()
    cur.close(); conn.close()
    return dict(u) if u else None

def safe_user(u):
    """Retirer le hash du mot de passe avant d'envoyer au client"""
    if u:
        u = dict(u)
        u.pop('password_hash', None)
    return u


# ═══════════════════════════════════════════════
#  AUTH — CONNEXION UNIFIÉE (3 RÔLES)
# ═══════════════════════════════════════════════
@app.route("/api/auth", methods=["POST"])
def auth():
    """
    Client  → téléphone seul (création auto si nouveau)
    Livreur → téléphone + code_livreur
    Admin   → téléphone + password
    """
    data      = request.get_json()
    telephone = data.get("telephone", "").strip()
    role      = data.get("role", "client").strip()       # client | livreur | admin
    password  = data.get("password", "").strip()
    code_liv  = data.get("code_livreur", "").strip()

    if not telephone or len(telephone) < 8:
        return jsonify({"error": "Numéro invalide"}), 400

    conn = get_conn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ── CLIENT ──────────────────────────────
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
        cur.close(); conn.close()
        return jsonify({"success": True, "role": "client", "user": safe_user(user)}), 200

    # ── LIVREUR ─────────────────────────────
    if role == "livreur":
        if not code_liv:
            return jsonify({"error": "Code livreur requis"}), 400
        cur.execute(
            "SELECT * FROM users WHERE telephone=%s AND role='livreur' AND code_livreur=%s AND actif=TRUE",
            (telephone, code_liv)
        )
        user = cur.fetchone()
        cur.close(); conn.close()
        if not user:
            return jsonify({"error": "Identifiants livreur incorrects"}), 401
        return jsonify({"success": True, "role": "livreur", "user": safe_user(user)}), 200

    # ── ADMIN ───────────────────────────────
    if role == "admin":
        if not password:
            return jsonify({"error": "Mot de passe requis"}), 400
        cur.execute(
            "SELECT * FROM users WHERE telephone=%s AND role='admin' AND password_hash=%s",
            (telephone, hash_pwd(password))
        )
        user = cur.fetchone()
        cur.close(); conn.close()
        if not user:
            return jsonify({"error": "Identifiants admin incorrects"}), 401
        return jsonify({"success": True, "role": "admin", "user": safe_user(user)}), 200

    return jsonify({"error": "Rôle non reconnu"}), 400


# ═══════════════════════════════════════════════
#  CLIENT — PASSER DES COMMANDES
# ═══════════════════════════════════════════════
@app.route("/api/colis/recuperer", methods=["POST"])
def recuperer_colis():
    data = request.get_json()
    telephone         = data.get("telephone","").strip()
    lieu_recuperation = data.get("lieu_recuperation","").strip()
    description_colis = data.get("description_colis","").strip()
    adresse_client    = data.get("adresse_client","").strip()
    note              = data.get("note_supplementaire","")

    if not all([telephone, lieu_recuperation, description_colis, adresse_client]):
        return jsonify({"error": "Champs obligatoires manquants"}), 400

    user = get_user_by_tel(telephone)
    if not user:
        return jsonify({"error": "Client introuvable"}), 404

    try:
        code = gen_code()
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO colis (code_suivi, type_service, client_id, telephone_client,
                lieu_recuperation, description_colis, adresse_client,
                note_supplementaire, statut, frais_livraison)
            VALUES (%s,'recuperation',%s,%s,%s,%s,%s,%s,'en_attente',500) RETURNING *
        """, (code, user["id"], telephone, lieu_recuperation,
              description_colis, adresse_client, note))
        colis = dict(cur.fetchone())
        add_suivi(conn, colis["id"], "en_attente",
                  "Demande de récupération reçue.", user["id"])
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success":True,"code_suivi":code,"colis":colis}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/colis/acheter", methods=["POST"])
def acheter_colis():
    data = request.get_json()
    telephone      = data.get("telephone","").strip()
    article        = data.get("article","").strip()
    boutique       = data.get("boutique","").strip()
    budget_article = data.get("budget_article", 0)
    adresse_client = data.get("adresse_client","").strip()
    note           = data.get("note_supplementaire","")

    if not all([telephone, article, boutique, adresse_client]):
        return jsonify({"error": "Champs obligatoires manquants"}), 400

    user = get_user_by_tel(telephone)
    if not user:
        return jsonify({"error": "Client introuvable"}), 404

    try:
        code = gen_code()
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO colis (code_suivi, type_service, client_id, telephone_client,
                article, boutique, budget_article, adresse_client,
                note_supplementaire, statut, frais_livraison)
            VALUES (%s,'achat',%s,%s,%s,%s,%s,%s,%s,'en_attente',700) RETURNING *
        """, (code, user["id"], telephone, article, boutique,
              budget_article, adresse_client, note))
        colis = dict(cur.fetchone())
        add_suivi(conn, colis["id"], "en_attente",
                  f"Commande reçue pour '{article}'.", user["id"])
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success":True,"code_suivi":code,"colis":colis}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/colis/livrer", methods=["POST"])
def livrer_colis():
    data = request.get_json()
    telephone         = data.get("telephone","").strip()
    nom_destinataire  = data.get("nom_destinataire","").strip()
    telephone_dest    = data.get("telephone_dest","").strip()
    adresse_livraison = data.get("adresse_livraison","").strip()
    description_envoi = data.get("description_envoi","").strip()
    adresse_client    = data.get("adresse_client","").strip()
    note              = data.get("note_supplementaire","")

    if not all([telephone, nom_destinataire, telephone_dest,
                adresse_livraison, description_envoi, adresse_client]):
        return jsonify({"error": "Champs obligatoires manquants"}), 400

    user = get_user_by_tel(telephone)
    if not user:
        return jsonify({"error": "Client introuvable"}), 404

    try:
        code = gen_code()
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO colis (code_suivi, type_service, client_id, telephone_client,
                nom_destinataire, telephone_dest, adresse_livraison,
                description_envoi, adresse_client, note_supplementaire,
                statut, frais_livraison)
            VALUES (%s,'livraison',%s,%s,%s,%s,%s,%s,%s,%s,'en_attente',500) RETURNING *
        """, (code, user["id"], telephone, nom_destinataire, telephone_dest,
              adresse_livraison, description_envoi, adresse_client, note))
        colis = dict(cur.fetchone())
        add_suivi(conn, colis["id"], "en_attente",
                  f"Livraison à {nom_destinataire} enregistrée.", user["id"])
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success":True,"code_suivi":code,"colis":colis}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Suivi public ───────────────────────────
@app.route("/api/colis/suivi/<code>", methods=["GET"])
def suivi_colis(code):
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM colis WHERE code_suivi=%s", (code.upper(),))
        colis = cur.fetchone()
        if not colis:
            return jsonify({"error": "Code introuvable"}), 404
        cur.execute("""
            SELECT s.statut, s.message, s.created_at,
                   u.nom AS auteur_nom, u.role AS auteur_role
            FROM suivi_statuts s
            LEFT JOIN users u ON u.id = s.auteur_id
            WHERE s.colis_id=%s ORDER BY s.created_at DESC
        """, (colis["id"],))
        historique = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({
            "success": True,
            "colis": dict(colis),
            "historique": [dict(h) for h in historique]
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Historique client ──────────────────────
@app.route("/api/client/<telephone>/historique", methods=["GET"])
def historique_client(telephone):
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.*, u.nom AS livreur_nom
            FROM colis c
            LEFT JOIN users u ON u.id = c.livreur_id
            WHERE c.telephone_client=%s
            ORDER BY c.created_at DESC
        """, (telephone,))
        rows = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"success": True, "commandes": [dict(r) for r in rows]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════
#  LIVREUR — MES MISSIONS
# ═══════════════════════════════════════════════
@app.route("/api/livreur/<int:livreur_id>/missions", methods=["GET"])
def missions_livreur(livreur_id):
    """Retourne tous les colis assignés à ce livreur"""
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT c.*, u.telephone AS client_tel
            FROM colis c
            JOIN users u ON u.id = c.client_id
            WHERE c.livreur_id=%s
            ORDER BY c.created_at DESC
        """, (livreur_id,))
        missions = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"success": True, "missions": [dict(m) for m in missions]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/livreur/colis/<code>/statut", methods=["PUT"])
def livreur_update_statut(code):
    """Le livreur met à jour le statut d'un colis assigné"""
    data       = request.get_json()
    statut     = data.get("statut","").strip()
    message    = data.get("message","")
    livreur_id = data.get("livreur_id")

    STATUTS_LIVREUR = [
        "en_route_recuperation", "recupere", "en_livraison", "livre"
    ]
    if statut not in STATUTS_LIVREUR:
        return jsonify({"error": f"Statut non autorisé pour un livreur: {STATUTS_LIVREUR}"}), 400

    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Vérifier que ce colis est bien assigné à ce livreur
        cur.execute(
            "SELECT * FROM colis WHERE code_suivi=%s AND livreur_id=%s",
            (code.upper(), livreur_id)
        )
        colis = cur.fetchone()
        if not colis:
            return jsonify({"error": "Colis introuvable ou non assigné à vous"}), 403

        cur.execute(
            "UPDATE colis SET statut=%s, updated_at=NOW() WHERE id=%s RETURNING *",
            (statut, colis["id"])
        )
        updated = dict(cur.fetchone())
        add_suivi(conn, colis["id"], statut,
                  message or f"Statut mis à jour par livreur: {statut}", livreur_id)
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "colis": updated}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════
#  ADMIN — GESTION COMPLÈTE
# ═══════════════════════════════════════════════

# ── Dashboard stats ─────────────────────────
@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("SELECT COUNT(*) AS total FROM colis")
        total = cur.fetchone()["total"]

        cur.execute("SELECT COUNT(*) AS nb FROM colis WHERE statut='en_attente'")
        en_attente = cur.fetchone()["nb"]

        cur.execute("SELECT COUNT(*) AS nb FROM colis WHERE statut='en_livraison'")
        en_livraison = cur.fetchone()["nb"]

        cur.execute("SELECT COUNT(*) AS nb FROM colis WHERE statut='livre'")
        livres = cur.fetchone()["nb"]

        cur.execute("SELECT COUNT(*) AS nb FROM users WHERE role='client'")
        nb_clients = cur.fetchone()["nb"]

        cur.execute("SELECT COUNT(*) AS nb FROM users WHERE role='livreur' AND actif=TRUE")
        nb_livreurs = cur.fetchone()["nb"]

        cur.execute("SELECT COALESCE(SUM(frais_livraison),0) AS total FROM colis WHERE statut='livre'")
        revenus = cur.fetchone()["total"]

        cur.execute("""
            SELECT type_service, COUNT(*) AS nb
            FROM colis GROUP BY type_service
        """)
        par_type = cur.fetchall()

        cur.close(); conn.close()
        return jsonify({
            "success": True,
            "stats": {
                "total_colis":   total,
                "en_attente":    en_attente,
                "en_livraison":  en_livraison,
                "livres":        livres,
                "nb_clients":    nb_clients,
                "nb_livreurs":   nb_livreurs,
                "revenus_fcfa":  float(revenus),
                "par_type":      [dict(r) for r in par_type],
            }
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Tous les colis ───────────────────────────
@app.route("/api/admin/colis", methods=["GET"])
def admin_all_colis():
    statut = request.args.get("statut")  # filtre optionnel
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        if statut:
            cur.execute("""
                SELECT c.*, u.telephone AS client_tel,
                       l.nom AS livreur_nom, l.telephone AS livreur_tel
                FROM colis c
                JOIN users u ON u.id = c.client_id
                LEFT JOIN users l ON l.id = c.livreur_id
                WHERE c.statut=%s ORDER BY c.created_at DESC
            """, (statut,))
        else:
            cur.execute("""
                SELECT c.*, u.telephone AS client_tel,
                       l.nom AS livreur_nom, l.telephone AS livreur_tel
                FROM colis c
                JOIN users u ON u.id = c.client_id
                LEFT JOIN users l ON l.id = c.livreur_id
                ORDER BY c.created_at DESC
            """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"success": True, "colis": [dict(r) for r in rows]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Assigner un livreur ──────────────────────
@app.route("/api/admin/colis/<code>/assigner", methods=["PUT"])
def assigner_livreur(code):
    data       = request.get_json()
    livreur_id = data.get("livreur_id")
    if not livreur_id:
        return jsonify({"error": "livreur_id requis"}), 400
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "UPDATE colis SET livreur_id=%s, statut='confirme', updated_at=NOW() "
            "WHERE code_suivi=%s RETURNING *",
            (livreur_id, code.upper())
        )
        colis = cur.fetchone()
        if not colis:
            return jsonify({"error": "Colis introuvable"}), 404
        add_suivi(conn, colis["id"], "confirme",
                  "Livreur assigné. Colis confirmé.", livreur_id)
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "colis": dict(colis)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Mettre à jour n'importe quel statut ──────
STATUTS_VALIDES = [
    "en_attente","confirme","en_route_recuperation",
    "recupere","en_livraison","livre","annule"
]

@app.route("/api/admin/colis/<code>/statut", methods=["PUT"])
def admin_update_statut(code):
    data    = request.get_json()
    statut  = data.get("statut","").strip()
    message = data.get("message","")
    admin_id= data.get("admin_id")
    if statut not in STATUTS_VALIDES:
        return jsonify({"error": f"Statut invalide. Valides: {STATUTS_VALIDES}"}), 400
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "UPDATE colis SET statut=%s, updated_at=NOW() WHERE code_suivi=%s RETURNING *",
            (statut, code.upper())
        )
        colis = cur.fetchone()
        if not colis:
            return jsonify({"error": "Colis introuvable"}), 404
        add_suivi(conn, colis["id"], statut,
                  message or f"Admin: statut → {statut}", admin_id)
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "colis": dict(colis)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Créer un livreur ─────────────────────────
@app.route("/api/admin/livreurs", methods=["POST"])
def creer_livreur():
    data      = request.get_json()
    telephone = data.get("telephone","").strip()
    nom       = data.get("nom","").strip()
    if not telephone or not nom:
        return jsonify({"error": "telephone et nom requis"}), 400
    try:
        code_liv = gen_code_livreur()
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            INSERT INTO users (telephone, role, nom, code_livreur)
            VALUES (%s,'livreur',%s,%s) RETURNING *
        """, (telephone, nom, code_liv))
        livreur = dict(cur.fetchone())
        conn.commit(); cur.close(); conn.close()
        livreur.pop('password_hash', None)
        return jsonify({
            "success": True,
            "livreur": livreur,
            "code_livreur": code_liv,
            "message": f"Livreur créé. Code d'accès : {code_liv}"
        }), 201
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Ce numéro existe déjà"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Liste des livreurs ───────────────────────
@app.route("/api/admin/livreurs", methods=["GET"])
def liste_livreurs():
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
        cur.close(); conn.close()
        return jsonify({"success": True, "livreurs": [dict(l) for l in livreurs]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Désactiver / réactiver un livreur ────────
@app.route("/api/admin/livreurs/<int:livreur_id>/actif", methods=["PUT"])
def toggle_livreur(livreur_id):
    data  = request.get_json()
    actif = data.get("actif", True)
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "UPDATE users SET actif=%s WHERE id=%s AND role='livreur' RETURNING id, nom, actif",
            (actif, livreur_id)
        )
        u = cur.fetchone()
        conn.commit(); cur.close(); conn.close()
        if not u:
            return jsonify({"error": "Livreur introuvable"}), 404
        return jsonify({"success": True, "livreur": dict(u)}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Liste des clients ────────────────────────
@app.route("/api/admin/clients", methods=["GET"])
def liste_clients():
    try:
        conn = get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT u.id, u.telephone, u.created_at,
                   COUNT(c.id) AS nb_commandes
            FROM users u
            LEFT JOIN colis c ON c.client_id = u.id
            WHERE u.role='client'
            GROUP BY u.id ORDER BY u.created_at DESC
        """)
        clients = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"success": True, "clients": [dict(c) for c in clients]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# SANTÉ
# ─────────────────────────────────────────────
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "app": "FABLA v2", "ville": "Assinie-Mafia"}), 200


try:
    init_db()
    print("✅ Base de données initialisée")
except Exception as e:
    print(f"⚠️ Erreur init DB: {e}")

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
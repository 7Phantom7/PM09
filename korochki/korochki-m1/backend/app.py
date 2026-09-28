import os

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_caching import Cache
from flask_compress import Compress
import pymysql
from werkzeug.security import generate_password_hash, check_password_hash
import re

app = Flask(__name__)
CORS(app)
Compress(app) # Оптимизация: сжатие ответов (Gzip/Brotli)

# Оптимизация: серверное кэширование
cache = Cache(config={'CACHE_TYPE': 'SimpleCache', 'CACHE_DEFAULT_TIMEOUT': 300})
cache.init_app(app)

DB_CONFIG = {
    "host": "127.0.0.1",
    "user": "root",
    "password": "",
    "database": "korochki",
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}

def get_db_connection():
    return pymysql.connect(**DB_CONFIG)

@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json()
    login = data.get('login', '')
    password = data.get('password', '')
    full_name = data.get('full_name', '')
    phone = data.get('phone', '')
    email = data.get('email', '')

    if not re.match(r'^[A-Za-z0-9]{6,}$', login):
        return jsonify({"error": "Invalid login"}), 400
    if len(password) < 8:
        return jsonify({"error": "Invalid password"}), 400
    if not re.match(r'^[А-Яа-яЁё\s\-]+$', full_name):
        return jsonify({"error": "Invalid name"}), 400
    if not re.match(r'^8\(\d{3}\)\d{3}-\d{2}-\d{2}$', phone):
        return jsonify({"error": "Invalid phone"}), 400
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return jsonify({"error": "Invalid email"}), 400

    password_hash = generate_password_hash(password)

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (login, password_hash, full_name, phone, email, role_id) VALUES (%s, %s, %s, %s, %s, 2)",
                (login, password_hash, full_name, phone, email)
            )
            user_id = cur.lastrowid
        conn.commit()
        conn.close()
        return jsonify({"success": True, "user_id": user_id}), 200
    except pymysql.err.IntegrityError:
        return jsonify({"error": "Unique constraint failed"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    login = data.get('login', '')
    password = data.get('password', '')

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT id, password_hash, role_id FROM users WHERE login = %s", (login,))
        user = cur.fetchone()
    conn.close()

    if user and check_password_hash(user['password_hash'], password):
        return jsonify({"success": True, "user_id": user['id'], "role_id": user['role_id']}), 200
    else:
        return jsonify({"error": "Unauthorized"}), 401

@app.route('/api/workshops', methods=['GET'])
def get_workshops():
    # Курсорная пагинация
    last_id = request.args.get('last_id', 0, type=int)
    limit = request.args.get('limit', 100, type=int)
    
    conn = get_db_connection()
    with conn.cursor() as cur:
        # Используем WHERE id > :last_id вместо медленного OFFSET
        cur.execute("""
            SELECT * FROM workshops 
            WHERE id > %s 
            ORDER BY id ASC 
            LIMIT %s
        """, (last_id, limit))
        workshops = cur.fetchall()
    conn.close()
    return jsonify(workshops), 200

@app.route('/api/payment_methods', methods=['GET'])
@cache.cached(timeout=600) # Кэшируем справочник на 10 минут
def get_payment_methods():
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM payment_methods")
        methods = cur.fetchall()
    conn.close()
    return jsonify(methods), 200

@app.route('/api/applications', methods=['POST'])
def create_application():
    data = request.get_json()
    user_id = data.get('user_id')
    workshop_id = data.get('workshop_id')
    start_date = data.get('start_date')
    payment_method_id = data.get('payment_method_id')

    if not all([user_id, workshop_id, start_date, payment_method_id]):
        return jsonify({"error": "Missing required fields"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT place_count FROM workshops WHERE id = %s", (workshop_id,))
            workshop = cur.fetchone()

            if not workshop or workshop['place_count'] <= 0:
                conn.close()
                return jsonify({"error": "No available places in this workshop"}), 400

            cur.execute(
                "INSERT INTO applications (user_id, workshop_id, start_date, payment_method_id, status_id) VALUES (%s, %s, %s, %s, 1)",
                (user_id, workshop_id, start_date, payment_method_id)
            )
            app_id = cur.lastrowid

            cur.execute(
                "UPDATE workshops SET place_count = place_count - 1 WHERE id = %s",
                (workshop_id,)
            )

        conn.commit()
        conn.close()
        return jsonify({"success": True, "application_id": app_id}), 201
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({"error": str(e)}), 500

@app.route('/api/applications/my', methods=['GET'])
def get_my_applications():
    user_id = request.args.get('user_id')
    if not user_id:
        return jsonify({"error": "User ID required"}), 400

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.id, w.topic AS course_name, a.start_date, pm.name AS payment_method, s.name AS status
            FROM applications a
            JOIN workshops w ON a.workshop_id = w.id
            JOIN payment_methods pm ON a.payment_method_id = pm.id
            JOIN application_statuses s ON a.status_id = s.id
            WHERE a.user_id = %s
            ORDER BY a.created_at DESC
        """, (user_id,))
        apps = cur.fetchall()
    conn.close()
    return jsonify(apps), 200

@app.route('/api/admin/applications', methods=['GET'])
def get_all_applications():
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.id, u.login, u.full_name, w.topic AS course_name, a.start_date, pm.name AS payment_method, s.name AS status, a.status_id
            FROM applications a
            JOIN users u ON a.user_id = u.id
            JOIN workshops w ON a.workshop_id = w.id
            JOIN payment_methods pm ON a.payment_method_id = pm.id
            JOIN application_statuses s ON a.status_id = s.id
            ORDER BY a.created_at DESC
        """)
        apps = cur.fetchall()
    conn.close()
    return jsonify(apps), 200

@app.route('/api/admin/applications/<int:app_id>/status', methods=['PATCH'])
def update_application_status(app_id):
    data = request.get_json()
    status_id = data.get('status_id')

    if not status_id:
        return jsonify({"error": "Status ID required"}), 400

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("UPDATE applications SET status_id = %s WHERE id = %s", (status_id, app_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 200

@app.route('/api/reviews', methods=['POST'])
def add_review():
    data = request.get_json()
    user_id = data.get('user_id')
    text = data.get('text', '')

    if not user_id or not text.strip():
        return jsonify({"error": "Invalid review data"}), 400

    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO reviews (user_id, text) VALUES (%s, %s)", (user_id, text))
    conn.commit()
    conn.close()
    return jsonify({"success": True}), 201

FRONTEND_FOLDER = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'frontend'))

@app.route('/')
def index():
    return send_from_directory(FRONTEND_FOLDER, 'portfolio.html')

@app.route('/<path:path>')
def serve_frontend(path):
    return send_from_directory(FRONTEND_FOLDER, path)

if __name__ == '__main__':
    app.run(debug=True)
"""
Muro de Saludos — AWS Student Community Day Chile 2026
=======================================================

App web en Flask que permite a los estudiantes publicar un saludo con su foto.
Las fotos se almacenan en Amazon S3 y los metadatos (nombre, comunidad, mensaje)
en una base de datos SQLite local.

Ejecutar:
    python app.py                 # desarrollo (puerto 5000)
    gunicorn -w 2 -b 0.0.0.0:5000 app:app   # producción

Configuración: ver archivo .env.example
"""

import os
import re
import uuid
import sqlite3
from datetime import datetime

import boto3
from botocore.exceptions import ClientError, BotoCoreError
from botocore.config import Config
from dotenv import load_dotenv
from flask import (
    Flask, request, redirect, url_for, render_template, flash, g
)
from werkzeug.utils import secure_filename

# ------------------------------------------------------------------
# Configuración
# ------------------------------------------------------------------
load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "")
S3_PREFIX = os.getenv("S3_PREFIX", "saludos/")
IMAGE_URL_MODE = os.getenv("IMAGE_URL_MODE", "presigned").lower()  # presigned | public
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

# Credenciales: si están en el .env se usan; si no, boto3 busca el IAM Role de la EC2.
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID") or None
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY") or None

# Límites de subida
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB por foto
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_MIME = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
}

PRESIGN_EXPIRY = 3600  # segundos de validez de las URLs firmadas

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saludos.db")

# ------------------------------------------------------------------
# App Flask
# ------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


# ------------------------------------------------------------------
# Cliente S3 (perezoso)
# ------------------------------------------------------------------
def get_s3_client():
    """Crea un cliente S3. Usa claves del .env si existen; si no, el IAM Role."""
    session_kwargs = {"region_name": AWS_REGION}
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        session_kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
        session_kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
    return boto3.client(
        "s3",
        config=Config(signature_version="s3v4"),
        **session_kwargs,
    )


# ------------------------------------------------------------------
# Base de datos SQLite
# ------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS saludos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre      TEXT NOT NULL,
            comunidad   TEXT NOT NULL,
            mensaje     TEXT NOT NULL,
            s3_key      TEXT,
            created_at  TEXT NOT NULL
        )
        """
    )
    db.commit()
    db.close()


# ------------------------------------------------------------------
# Utilidades
# ------------------------------------------------------------------
def allowed_file(filename, mimetype):
    ext_ok = (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )
    mime_ok = mimetype in ALLOWED_MIME
    return ext_ok and mime_ok


def clean_text(value, max_len):
    """Normaliza y recorta texto del usuario para evitar entradas abusivas."""
    value = (value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:max_len]


def build_image_url(s3_key):
    """Devuelve la URL para mostrar la imagen según el modo configurado."""
    if not s3_key:
        return None
    if IMAGE_URL_MODE == "public":
        # URL pública directa (requiere bucket/objeto con lectura pública)
        return f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
    # Modo recomendado: URL firmada temporal (bucket privado)
    try:
        s3 = get_s3_client()
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": s3_key},
            ExpiresIn=PRESIGN_EXPIRY,
        )
    except (ClientError, BotoCoreError):
        return None


def upload_to_s3(file_storage):
    """Sube el archivo a S3 y devuelve la s3_key. Lanza excepción si falla."""
    original = secure_filename(file_storage.filename or "")
    ext = original.rsplit(".", 1)[1].lower() if "." in original else "jpg"
    key = f"{S3_PREFIX}{uuid.uuid4().hex}.{ext}"

    extra_args = {"ContentType": file_storage.mimetype}
    # En modo público marcamos el objeto como de lectura pública.
    # (El bucket debe permitir ACLs; ver README. En modo presigned NO se usa.)
    if IMAGE_URL_MODE == "public":
        extra_args["ACL"] = "public-read"

    s3 = get_s3_client()
    s3.upload_fileobj(
        file_storage,
        S3_BUCKET,
        key,
        ExtraArgs=extra_args,
    )
    return key


# ------------------------------------------------------------------
# Rutas
# ------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM saludos ORDER BY id DESC"
    ).fetchall()

    saludos = []
    for row in rows:
        saludos.append(
            {
                "nombre": row["nombre"],
                "comunidad": row["comunidad"],
                "mensaje": row["mensaje"],
                "image_url": build_image_url(row["s3_key"]),
                "created_at": row["created_at"],
            }
        )
    return render_template("index.html", saludos=saludos)


@app.route("/publicar", methods=["POST"])
def publicar():
    nombre = clean_text(request.form.get("nombre"), 80)
    comunidad = clean_text(request.form.get("comunidad"), 120)
    mensaje = clean_text(request.form.get("mensaje"), 500)
    foto = request.files.get("foto")

    # Validaciones
    if not nombre or not comunidad or not mensaje:
        flash("Por favor completa nombre, comunidad y mensaje.", "error")
        return redirect(url_for("index"))

    if not S3_BUCKET:
        flash("La app no tiene configurado el bucket S3. Revisa el archivo .env.", "error")
        return redirect(url_for("index"))

    s3_key = None
    if foto and foto.filename:
        if not allowed_file(foto.filename, foto.mimetype):
            flash("Formato de imagen no permitido. Usa PNG, JPG, GIF o WEBP.", "error")
            return redirect(url_for("index"))
        try:
            s3_key = upload_to_s3(foto)
        except (ClientError, BotoCoreError) as e:
            app.logger.error("Error subiendo a S3: %s", e)
            flash("No se pudo subir la foto a S3. Revisa credenciales y permisos.", "error")
            return redirect(url_for("index"))

    db = get_db()
    db.execute(
        "INSERT INTO saludos (nombre, comunidad, mensaje, s3_key, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (nombre, comunidad, mensaje, s3_key, datetime.utcnow().isoformat(timespec="seconds")),
    )
    db.commit()
    flash("Saludo publicado con éxito.", "ok")
    return redirect(url_for("index"))


@app.route("/health")
def health():
    return {"status": "ok"}, 200


@app.errorhandler(413)
def too_large(e):
    flash("La imagen es demasiado grande (máximo 5 MB).", "error")
    return redirect(url_for("index"))


@app.after_request
def agregar_cabeceras_de_seguridad(response):
    """
    Checklist de Seguridad: Cabeceras HTTP para proteccion contra ataques web
    comunes y calificacion 'A' en securityheaders.com.
    """
    # Proteccion contra Clickjacking (evita embebido en iframes maliciosos)
    response.headers["X-Frame-Options"] = "DENY"
    # Evita que el navegador adivine el tipo MIME (MIME-Sniffing)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Content Security Policy: restringe origenes permitidos para scripts, estilos y fuentes
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com;"
    )
    # Politica de referencia estricta
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Restringe acceso a APIs de hardware del navegador
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


# Inicializa la base de datos al importar el módulo (sirve también con gunicorn).
init_db()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)

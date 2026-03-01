import csv
import io
import os
import re
import secrets
import urllib.parse
from datetime import datetime, time, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, text
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32).hex()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE", "0") == "1"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

DB_PATH = os.path.join(INSTANCE_DIR, "turnos.db")
database_url = (os.getenv("DATABASE_URL") or "").strip()
if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"

ADMIN_USER = os.getenv("ADMIN_USER", "admin").strip()
ADMIN_PASS_FALLBACK = os.getenv("ADMIN_PASS", "").strip()
ADMIN_PASSWORD_HASH = (os.getenv("ADMIN_PASSWORD_HASH") or "").strip()
if not ADMIN_PASSWORD_HASH:
    fallback = ADMIN_PASS_FALLBACK or "1234"
    ADMIN_PASSWORD_HASH = generate_password_hash(fallback, method="pbkdf2:sha256")

GA4_MEASUREMENT_ID = (os.getenv("GA4_MEASUREMENT_ID") or "").strip()
PLAUSIBLE_DOMAIN = (os.getenv("PLAUSIBLE_DOMAIN") or "").strip()
REMINDER_TASK_TOKEN = (os.getenv("REMINDER_TASK_TOKEN") or "").strip()

MONTO_SENA = 10000
MP_ALIAS = "Mercado.P"
MP_TITULAR = "Carmen Maria De La Concepción"
WHATSAPP_NUM = "5491123869037"

HORA_APERTURA = time(9, 30)
HORA_CIERRE = time(20, 0)
TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")
DEFAULT_CAPACIDAD = 2

SUCURSALES = [
    {"id": 1, "nombre": "Puerto Norte", "direccion": "Av. Brisas del Lago 1240", "capacidad": 2},
    {"id": 2, "nombre": "Jardin Central", "direccion": "Calle Los Ombues 875", "capacidad": 2},
]
SUCURSAL_MAP = {s["id"]: s for s in SUCURSALES}

ESTADOS_TURNO = ["pendiente", "confirmado", "cancelado", "asistio"]
ESTADOS_ACTIVOS = ["pendiente", "confirmado", "asistio"]

db = SQLAlchemy(app)


class Servicio(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    activo = db.Column(db.Boolean, default=True)


class ServicioOpcion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    servicio_id = db.Column(db.Integer, db.ForeignKey("servicio.id"), nullable=False)
    duracion = db.Column(db.Integer, nullable=False)
    precio = db.Column(db.Integer, nullable=False)
    activo = db.Column(db.Boolean, default=True)

    servicio = db.relationship("Servicio", backref="opciones")


class Turno(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    sucursal = db.Column(db.String(60), nullable=False)

    opcion_id = db.Column(db.Integer, db.ForeignKey("servicio_opcion.id"), nullable=False)
    servicio_nombre = db.Column(db.String(120), nullable=False)
    duracion = db.Column(db.Integer, nullable=False)
    precio = db.Column(db.Integer, nullable=False)
    inicio = db.Column(db.DateTime, nullable=False)
    fin = db.Column(db.DateTime, nullable=False)
    telefono = db.Column(db.String(30), nullable=False, default="")
    observacion = db.Column(db.Text, nullable=True, default="")
    estado = db.Column(db.String(20), nullable=False, default="pendiente")
    recordatorio_enviado_en = db.Column(db.DateTime, nullable=True)

    creado_en = db.Column(db.DateTime, default=datetime.now)


class Sucursal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(60), unique=True, nullable=False)
    direccion = db.Column(db.String(160), nullable=False)
    capacidad = db.Column(db.Integer, nullable=False, default=2)
    activa = db.Column(db.Boolean, default=True)


def _parse_ymd(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _parse_int(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def ahora_local_naive():
    return datetime.now(TZ_AR).replace(tzinfo=None)


def normalizar_telefono(raw: str) -> str:
    return re.sub(r"\D+", "", raw or "")


def capacidad_sucursal(nombre: str) -> int:
    suc = Sucursal.query.filter_by(nombre=nombre, activa=True).first()
    if suc:
        return int(suc.capacidad)
    return DEFAULT_CAPACIDAD


def overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_end > b_start and a_start < b_end


def hay_capacidad_para_turno(sucursal: str, inicio: datetime, fin: datetime) -> bool:
    solapados = Turno.query.filter(
        Turno.sucursal == sucursal,
        Turno.estado.in_(ESTADOS_ACTIVOS),
        Turno.fin > inicio,
        Turno.inicio < fin,
    ).count()
    return solapados < capacidad_sucursal(sucursal)


def build_whatsapp_link(nombre: str, servicio: str, fecha_turno: datetime, sucursal: str, reminder=False):
    if reminder:
        texto_wa = (
            f"Hola {nombre}, te recordamos tu turno de {servicio} "
            f"el {fecha_turno.strftime('%d/%m a las %H:%M')} en {sucursal}."
        )
    else:
        texto_wa = (
            f"Hola, soy {nombre}. Ya hice la seña y adjunto comprobante de mi reserva "
            f"de {servicio} para el {fecha_turno.strftime('%d/%m a las %H:%M')} en {sucursal}."
        )
    return f"https://wa.me/{WHATSAPP_NUM}?text={urllib.parse.quote(texto_wa)}"


def ensure_schema_updates():
    dialect = db.engine.dialect.name
    if dialect == "sqlite":
        cols = {r[1] for r in db.session.execute(text("PRAGMA table_info(turno)")).fetchall()}
        if "estado" not in cols:
            db.session.execute(text("ALTER TABLE turno ADD COLUMN estado VARCHAR(20) DEFAULT 'pendiente'"))
        if "recordatorio_enviado_en" not in cols:
            db.session.execute(text("ALTER TABLE turno ADD COLUMN recordatorio_enviado_en DATETIME"))
        db.session.execute(text("UPDATE turno SET estado='pendiente' WHERE estado IS NULL OR estado=''"))
        db.session.commit()
    elif dialect == "postgresql":
        db.session.execute(text("ALTER TABLE turno ADD COLUMN IF NOT EXISTS estado VARCHAR(20)"))
        db.session.execute(text("ALTER TABLE turno ADD COLUMN IF NOT EXISTS recordatorio_enviado_en TIMESTAMP"))
        db.session.execute(text("UPDATE turno SET estado='pendiente' WHERE estado IS NULL OR estado=''"))
        db.session.commit()


def migrate_legacy_sucursal_names():
    legacy_names = {
        "Nuñez": "Puerto Norte",
        "Núñez": "Puerto Norte",
        "Villa Urquiza": "Jardin Central",
    }
    for old_name, new_name in legacy_names.items():
        db.session.query(Turno).filter(Turno.sucursal == old_name).update(
            {Turno.sucursal: new_name},
            synchronize_session=False,
        )
        old_row = Sucursal.query.filter_by(nombre=old_name).first()
        new_row = Sucursal.query.filter_by(nombre=new_name).first()
        if old_row and not new_row:
            old_row.nombre = new_name
        elif old_row and new_row:
            db.session.delete(old_row)
    db.session.commit()


def seed_sucursales():
    for s in SUCURSALES:
        row = Sucursal.query.filter_by(nombre=s["nombre"]).first()
        if not row:
            db.session.add(
                Sucursal(
                    nombre=s["nombre"],
                    direccion=s["direccion"],
                    capacidad=int(s["capacidad"]),
                    activa=True,
                )
            )
        else:
            row.direccion = s["direccion"]
            row.capacidad = int(s["capacidad"])
            if row.activa is None:
                row.activa = True
    db.session.commit()


def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def verify_csrf_token():
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    saved = session.get("csrf_token")
    return bool(sent and saved and secrets.compare_digest(sent, saved))


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)

    return wrapper


@app.context_processor
def inject_globals():
    return {
        "csrf_token": get_csrf_token,
        "ga4_measurement_id": GA4_MEASUREMENT_ID,
        "plausible_domain": PLAUSIBLE_DOMAIN,
        "whatsapp_num": WHATSAPP_NUM,
    }


@app.before_request
def csrf_protect():
    if request.method != "POST":
        return
    if request.path.startswith("/tasks/"):
        return
    if not verify_csrf_token():
        abort(400, description="CSRF inválido. Recargá la página e intentá nuevamente.")


with app.app_context():
    db.create_all()
    ensure_schema_updates()
    migrate_legacy_sucursal_names()
    seed_sucursales()


@app.route("/")
def index():
    return render_template("index.html", sucursales=SUCURSALES)


@app.route("/sucursal/<int:id>")
def sucursal_detalle(id: int):
    suc = SUCURSAL_MAP.get(id)
    if not suc:
        return render_template("error.html", mensaje="Sucursal inválida.")

    opciones = (
        ServicioOpcion.query.join(Servicio)
        .filter(Servicio.activo.is_(True), ServicioOpcion.activo.is_(True))
        .order_by(Servicio.nombre, ServicioOpcion.duracion)
        .all()
    )

    return render_template(
        "reservar.html",
        sucursal=suc["nombre"],
        direccion=suc["direccion"],
        opciones=opciones,
    )


@app.route("/api/slots")
def api_slots():
    ymd = request.args.get("date", "").strip()
    sucursal = request.args.get("sucursal", "").strip()
    opcion_id = request.args.get("opcion_id", "").strip()

    if not ymd or not sucursal or not opcion_id:
        return jsonify({"ok": False, "error": "Faltan parámetros"}), 400

    d = _parse_ymd(ymd)
    if not d:
        return jsonify({"ok": False, "error": "Fecha inválida"}), 400

    opcion_id_int = _parse_int(opcion_id)
    if opcion_id_int is None:
        return jsonify({"ok": False, "error": "Opción inválida"}), 400

    opcion = db.session.get(ServicioOpcion, opcion_id_int)
    if not opcion or not opcion.activo or not opcion.servicio.activo:
        return jsonify({"ok": False, "error": "Opción no disponible"}), 400

    duracion = int(opcion.duracion)
    day_start = datetime.combine(d, time(0, 0))
    day_end = datetime.combine(d, time(23, 59, 59))

    turnos = (
        Turno.query.filter(
            Turno.sucursal == sucursal,
            Turno.estado.in_(ESTADOS_ACTIVOS),
            Turno.fin > day_start,
            Turno.inicio < day_end,
        )
        .order_by(Turno.inicio.asc())
        .all()
    )

    open_dt = datetime.combine(d, HORA_APERTURA)
    close_dt = datetime.combine(d, HORA_CIERRE)
    step = timedelta(minutes=15)
    delta = timedelta(minutes=duracion)

    slots = []
    t = open_dt
    while True:
        end = t + delta
        if end > close_dt:
            break

        solapados = sum(1 for x in turnos if overlaps(t, end, x.inicio, x.fin))
        if solapados < capacidad_sucursal(sucursal):
            if d > ahora_local_naive().date() or t >= ahora_local_naive().replace(second=0, microsecond=0):
                slots.append(t.strftime("%H:%M"))

        t += step

    return jsonify({"ok": True, "duracion": duracion, "slots": slots})


@app.route("/confirmar", methods=["POST"])
def confirmar():
    ahora = ahora_local_naive()
    if ahora.hour >= 23 or ahora.hour < 6:
        return render_template("error.html", mensaje="El horario para solicitar turnos comienza a las 06:00 am.")

    nombre = (request.form.get("nombre") or "").strip()
    telefono = normalizar_telefono(request.form.get("telefono"))
    sucursal = (request.form.get("sucursal") or "").strip()
    direccion = (request.form.get("direccion") or "").strip()
    opcion_id = request.form.get("opcion_id")
    fecha_turno_str = request.form.get("fecha_cita")

    try:
        if not nombre or not telefono or not sucursal or not opcion_id or not fecha_turno_str:
            return render_template("error.html", mensaje="Faltan datos obligatorios.")

        fecha_turno = datetime.strptime(fecha_turno_str, "%Y-%m-%dT%H:%M")
        opcion_id_int = _parse_int(opcion_id)
        if opcion_id_int is None:
            return render_template("error.html", mensaje="Servicio inválido.")

        opcion = db.session.get(ServicioOpcion, opcion_id_int)
        if not opcion or not opcion.activo or not opcion.servicio.activo:
            return render_template("error.html", mensaje="La opción seleccionada no está disponible.")

        if fecha_turno < ahora:
            return render_template("error.html", mensaje="No podés elegir un horario que ya pasó.")

        if fecha_turno.time() < HORA_APERTURA or fecha_turno.time() >= HORA_CIERRE:
            return render_template("error.html", mensaje="El horario elegido está fuera del horario comercial.")

        fin_turno = fecha_turno + timedelta(minutes=opcion.duracion)
        if fin_turno.time() > HORA_CIERRE:
            return render_template("error.html", mensaje="El turno supera el horario de cierre.")

        # Previene sobre-reserva en escenarios concurrentes.
        if db.session.bind.dialect.name == "sqlite":
            db.session.execute(text("BEGIN IMMEDIATE"))
        else:
            db.session.query(Sucursal).filter_by(nombre=sucursal).with_for_update().first()

        if not hay_capacidad_para_turno(sucursal, fecha_turno, fin_turno):
            db.session.rollback()
            return render_template("error.html", mensaje="Ese horario se ocupó recién. Elegí otro disponible.")

        nuevo_turno = Turno(
            nombre=nombre,
            telefono=telefono,
            sucursal=sucursal,
            opcion_id=opcion.id,
            servicio_nombre=opcion.servicio.nombre,
            duracion=opcion.duracion,
            precio=opcion.precio,
            inicio=fecha_turno,
            fin=fin_turno,
            estado="pendiente",
        )
        db.session.add(nuevo_turno)
        db.session.commit()

        link_whatsapp = build_whatsapp_link(nombre, opcion.servicio.nombre, fecha_turno, sucursal)

        return render_template(
            "confirmar.html",
            nombre=nombre,
            inicio=fecha_turno,
            sucursal=sucursal,
            servicio=opcion.servicio.nombre,
            duracion=opcion.duracion,
            precio=opcion.precio,
            monto_sena=MONTO_SENA,
            alias=MP_ALIAS,
            titular=MP_TITULAR,
            direccion=direccion,
            link=link_whatsapp,
        )

    except Exception:
        app.logger.exception("Error al confirmar turno")
        db.session.rollback()
        return render_template("error.html", mensaje="Ocurrió un error al confirmar el turno.")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = (request.form.get("password") or "").strip()

        if u == ADMIN_USER and check_password_hash(ADMIN_PASSWORD_HASH, p):
            session["is_admin"] = True
            return redirect(url_for("admin_panel"))

        return render_template("admin_login.html", error="Usuario o contraseña incorrectos.")

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))


def _turnos_query_desde_filtros(filtros):
    q = Turno.query

    if filtros["from"]:
        try:
            df = datetime.strptime(filtros["from"], "%Y-%m-%d")
            q = q.filter(Turno.inicio >= df)
        except Exception:
            pass

    if filtros["to"]:
        try:
            dt = datetime.strptime(filtros["to"], "%Y-%m-%d") + timedelta(days=1)
            q = q.filter(Turno.inicio < dt)
        except Exception:
            pass

    if filtros["sucursal"]:
        q = q.filter(Turno.sucursal == filtros["sucursal"])

    if filtros["servicio"]:
        q = q.filter(Turno.servicio_nombre == filtros["servicio"])

    if filtros["estado"]:
        q = q.filter(Turno.estado == filtros["estado"])

    if filtros["cliente_id"]:
        cid = normalizar_telefono(filtros["cliente_id"])
        q = q.filter(Turno.telefono.contains(cid))

    return q


@app.route("/admin")
@admin_required
def admin_panel():
    filtros = {
        "from": request.args.get("from", ""),
        "to": request.args.get("to", ""),
        "sucursal": request.args.get("sucursal", ""),
        "servicio": request.args.get("servicio", ""),
        "estado": request.args.get("estado", ""),
        "cliente_id": request.args.get("cliente_id", ""),
    }

    q = _turnos_query_desde_filtros(filtros)
    turnos = q.order_by(Turno.inicio.desc()).all()

    tels = {t.telefono for t in turnos if t.telefono}
    visitas_por_tel = {}
    if tels:
        filas = (
            db.session.query(Turno.telefono, func.count(Turno.id))
            .filter(Turno.telefono.in_(list(tels)))
            .group_by(Turno.telefono)
            .all()
        )
        visitas_por_tel = {tel: cnt for tel, cnt in filas}

    base = Turno.query.all()
    sucursales = sorted({t.sucursal for t in base})
    servicios = sorted({t.servicio_nombre for t in base})

    kpi_estado = {e: 0 for e in ESTADOS_TURNO}
    for t in turnos:
        if t.estado in kpi_estado:
            kpi_estado[t.estado] += 1

    return render_template(
        "admin_panel.html",
        turnos=turnos,
        filtros=filtros,
        visitas_por_tel=visitas_por_tel,
        sucursales=sucursales,
        servicios=servicios,
        estados_turno=ESTADOS_TURNO,
        kpi_estado=kpi_estado,
    )


@app.route("/admin/export/turnos.csv")
@admin_required
def export_turnos_csv():
    filtros = {
        "from": request.args.get("from", ""),
        "to": request.args.get("to", ""),
        "sucursal": request.args.get("sucursal", ""),
        "servicio": request.args.get("servicio", ""),
        "estado": request.args.get("estado", ""),
        "cliente_id": request.args.get("cliente_id", ""),
    }
    turnos = _turnos_query_desde_filtros(filtros).order_by(Turno.id.asc()).all()

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        [
            "ID Turno",
            "Inicio",
            "Cliente",
            "Teléfono",
            "Sucursal",
            "Servicio",
            "Estado",
            "Visitas",
            "Duración (min)",
            "Precio",
            "Observación",
        ]
    )

    tels = {t.telefono for t in turnos if t.telefono}
    visitas_por_tel = {}
    if tels:
        filas = (
            db.session.query(Turno.telefono, func.count(Turno.id))
            .filter(Turno.telefono.in_(list(tels)))
            .group_by(Turno.telefono)
            .all()
        )
        visitas_por_tel = {tel: cnt for tel, cnt in filas}

    for t in turnos:
        writer.writerow(
            [
                t.id,
                t.inicio.strftime("%d/%m/%Y %H:%M"),
                t.nombre,
                t.telefono,
                t.sucursal,
                t.servicio_nombre,
                t.estado,
                visitas_por_tel.get(t.telefono, 1),
                t.duracion,
                t.precio,
                (t.observacion or ""),
            ]
        )

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=turnos.csv"},
    )


@app.route("/admin/export/resumen_sucursal.csv")
@admin_required
def export_resumen_sucursal_csv():
    turnos = Turno.query.order_by(Turno.id.asc()).all()

    resumen = {}
    for t in turnos:
        r = resumen.setdefault(t.sucursal, {"cantidad": 0, "total": 0})
        r["cantidad"] += 1
        r["total"] += int(t.precio or 0)

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Sucursal", "Cantidad turnos", "Total $"])
    for s, data in sorted(resumen.items()):
        writer.writerow([s, data["cantidad"], data["total"]])

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=resumen_sucursal.csv"},
    )


@app.route("/admin/export/resumen_servicio.csv")
@admin_required
def export_resumen_servicio_csv():
    turnos = Turno.query.order_by(Turno.id.asc()).all()

    resumen = {}
    for t in turnos:
        r = resumen.setdefault(t.servicio_nombre, {"cantidad": 0, "total": 0})
        r["cantidad"] += 1
        r["total"] += int(t.precio or 0)

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Servicio", "Cantidad turnos", "Total $"])
    for sv, data in sorted(resumen.items()):
        writer.writerow([sv, data["cantidad"], data["total"]])

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=resumen_servicio.csv"},
    )


@app.route("/admin/export/resumen_clientes.csv")
@admin_required
def export_resumen_clientes_csv():
    turnos = Turno.query.order_by(Turno.id.asc()).all()

    resumen = {}
    for t in turnos:
        key = t.telefono or ""
        r = resumen.setdefault(key, {"nombre": t.nombre, "cantidad": 0, "total": 0})
        r["cantidad"] += 1
        r["total"] += int(t.precio or 0)

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["Teléfono", "Nombre", "Cantidad turnos", "Total $"])
    for tel, data in sorted(resumen.items(), key=lambda x: x[1]["total"], reverse=True):
        writer.writerow([tel, data["nombre"], data["cantidad"], data["total"]])

    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=resumen_clientes.csv"},
    )


@app.route("/admin/servicios", methods=["GET", "POST"])
@admin_required
def admin_servicios():
    if request.method == "POST":
        form_type = (request.form.get("form_type") or "servicio").strip()

        if form_type == "opcion":
            servicio_id = _parse_int(request.form.get("servicio_id"))
            duracion = _parse_int(request.form.get("duracion"))
            precio = _parse_int(request.form.get("precio"))
            activo = bool(request.form.get("activo"))

            servicio = db.session.get(Servicio, servicio_id) if servicio_id else None
            if servicio and duracion and precio is not None and duracion > 0 and precio >= 0:
                db.session.add(
                    ServicioOpcion(
                        servicio_id=servicio.id,
                        duracion=duracion,
                        precio=precio,
                        activo=activo,
                    )
                )
                db.session.commit()
        else:
            nombre = (request.form.get("nombre") or "").strip()
            activo = bool(request.form.get("activo"))
            if nombre:
                db.session.add(Servicio(nombre=nombre, activo=activo))
                db.session.commit()

        return redirect(url_for("admin_servicios"))

    servicios = Servicio.query.order_by(Servicio.nombre).all()
    opciones = ServicioOpcion.query.join(Servicio).order_by(Servicio.nombre, ServicioOpcion.duracion).all()
    return render_template("admin_servicios.html", servicios=servicios, opciones=opciones)


@app.route("/admin/opciones", methods=["GET", "POST"])
@admin_required
def admin_opciones():
    if request.method == "GET":
        return redirect(url_for("admin_servicios"))

    servicio_id = _parse_int(request.form.get("servicio_id"))
    duracion = _parse_int(request.form.get("duracion"))
    precio = _parse_int(request.form.get("precio"))
    activo = bool(request.form.get("activo"))

    if servicio_id and duracion and precio is not None:
        db.session.add(
            ServicioOpcion(
                servicio_id=servicio_id,
                duracion=duracion,
                precio=precio,
                activo=activo,
            )
        )
        db.session.commit()

    return redirect(url_for("admin_servicios"))


@app.route("/admin/turno/<int:turno_id>/observacion", methods=["POST"])
@admin_required
def admin_guardar_observacion(turno_id: int):
    t = Turno.query.get_or_404(turno_id)
    t.observacion = (request.form.get("observacion") or "").strip()
    db.session.commit()
    return redirect(request.referrer or url_for("admin_panel"))


@app.route("/admin/turno/<int:turno_id>/estado", methods=["POST"])
@admin_required
def admin_cambiar_estado(turno_id: int):
    t = Turno.query.get_or_404(turno_id)
    estado = (request.form.get("estado") or "").strip().lower()
    if estado not in ESTADOS_TURNO:
        return redirect(request.referrer or url_for("admin_panel"))

    t.estado = estado
    db.session.commit()
    return redirect(request.referrer or url_for("admin_panel"))


@app.route("/admin/reminders")
@admin_required
def admin_reminders():
    now = ahora_local_naive()
    start = now + timedelta(hours=23)
    end = now + timedelta(hours=25)

    turnos = (
        Turno.query.filter(
            Turno.inicio >= start,
            Turno.inicio <= end,
            Turno.estado.in_(["pendiente", "confirmado"]),
        )
        .order_by(Turno.inicio.asc())
        .all()
    )

    reminders = []
    for t in turnos:
        reminders.append(
            {
                "turno": t,
                "link": build_whatsapp_link(t.nombre, t.servicio_nombre, t.inicio, t.sucursal, reminder=True),
                "was_sent": bool(t.recordatorio_enviado_en),
            }
        )

    return render_template("admin_reminders.html", reminders=reminders)


@app.route("/tasks/reminders/run", methods=["POST"])
def task_run_reminders():
    if not REMINDER_TASK_TOKEN:
        return jsonify({"ok": False, "error": "Token de tarea no configurado"}), 503

    header_token = request.headers.get("X-Task-Token", "")
    if not secrets.compare_digest(header_token, REMINDER_TASK_TOKEN):
        return jsonify({"ok": False, "error": "No autorizado"}), 401

    now = ahora_local_naive()
    start = now + timedelta(hours=23)
    end = now + timedelta(hours=25)

    turnos = (
        Turno.query.filter(
            Turno.inicio >= start,
            Turno.inicio <= end,
            Turno.estado.in_(["pendiente", "confirmado"]),
            Turno.recordatorio_enviado_en.is_(None),
        )
        .all()
    )

    processed = []
    for t in turnos:
        t.recordatorio_enviado_en = now
        processed.append(
            {
                "turno_id": t.id,
                "telefono": t.telefono,
                "inicio": t.inicio.strftime("%Y-%m-%d %H:%M"),
                "whatsapp_link": build_whatsapp_link(t.nombre, t.servicio_nombre, t.inicio, t.sucursal, reminder=True),
            }
        )

    db.session.commit()

    return jsonify({"ok": True, "count": len(processed), "items": processed})


if __name__ == "__main__":
    app.run(debug=False, port=5001)

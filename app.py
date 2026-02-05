import os
import csv
import io
from flask import session
from functools import wraps
from flask import Response
import urllib.parse
from flask import jsonify
from datetime import date as date_type
from datetime import datetime, timedelta, time
from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy

print(">>> APP.PY CORRECTO CARGADO <<<")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "clave-temporal-cambiar-en-render")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

DB_PATH = os.path.join(INSTANCE_DIR, "turnos.db")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")  # cambiá esto en producción

db = SQLAlchemy(app)

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper



# ======================
# MODELOS
# ======================
class Servicio(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    activo = db.Column(db.Boolean, default=True)

class ServicioOpcion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    servicio_id = db.Column(db.Integer, db.ForeignKey("servicio.id"), nullable=False)
    duracion = db.Column(db.Integer, nullable=False)  # minutos
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

    creado_en = db.Column(db.DateTime, default=datetime.now)

class Sucursal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(60), unique=True, nullable=False)
    direccion = db.Column(db.String(160), nullable=False)
    capacidad = db.Column(db.Integer, nullable=False, default=2)
    activa = db.Column(db.Boolean, default=True)

with app.app_context():
    db.create_all()

# ======================
# CONFIG NEGOCIO
# ======================
HORA_APERTURA = time(9, 30)
HORA_CIERRE = time(20, 0)

# =======================
# CAPACIDAD POR SUCURSAL
# =======================
DEFAULT_CAPACIDAD = 2  # si no encuentra la sucursal, usa esto

CAPACIDAD_POR_SUCURSAL = {
    "Nuñez": 2,
    "Villa Urquiza": 2,
}

def capacidad_sucursal(nombre: str) -> int:
    return int(CAPACIDAD_POR_SUCURSAL.get(nombre, DEFAULT_CAPACIDAD))



SUCURSALES = [
    {"id": 1, "nombre": "Barrio Nuñez", "direccion": "Montañeses 2830 1b, CABA"},
    {"id": 2, "nombre": "Barrio Villa Urquiza", "direccion": "Monroe 5674 12b"},
]

SUCURSAL_MAP = {
    1: {"nombre": "Nuñez", "direccion": "Montañeses 2830 1b, CABA"},
    2: {"nombre": "Villa Urquiza", "direccion": "Monroe 5674 12b"},
}

MONTO_SENA = 10000
MP_ALIAS = "promasaje"
MP_TITULAR = "Irene Blasina Martínez Peña"
WHATSAPP_NUM = "5491151023354"


# ======================
# PUBLICO
# ======================
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
        .filter(Servicio.activo == True, ServicioOpcion.activo == True)
        .order_by(Servicio.nombre, ServicioOpcion.duracion)
        .all()
    )

    return render_template(
        "reservar.html",
        sucursal=suc["nombre"],
        direccion=suc["direccion"],
        opciones=opciones
    )


def _parse_ymd(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


@app.route("/api/slots")
def api_slots():
    # Params
    ymd = request.args.get("date", "").strip()           # YYYY-MM-DD
    sucursal = request.args.get("sucursal", "").strip()
    opcion_id = request.args.get("opcion_id", "").strip()

    if not ymd or not sucursal or not opcion_id:
        return jsonify({"ok": False, "error": "Faltan parámetros"}), 400

    d = _parse_ymd(ymd)
    if not d:
        return jsonify({"ok": False, "error": "Fecha inválida"}), 400

    opcion = ServicioOpcion.query.get(int(opcion_id))
    if not opcion or not opcion.activo or not opcion.servicio.activo:
        return jsonify({"ok": False, "error": "Opción no disponible"}), 400

    duracion = int(opcion.duracion)

    # Rango de día
    day_start = datetime.combine(d, time(0, 0))
    day_end = datetime.combine(d, time(23, 59, 59))

    # Traemos turnos del día (que se crucen con el día)
    turnos = Turno.query.filter(
        Turno.sucursal == sucursal,
        Turno.fin > day_start,
        Turno.inicio < day_end
    ).order_by(Turno.inicio.asc()).all()

    # Horarios laborales
    open_dt = datetime.combine(d, HORA_APERTURA)
    close_dt = datetime.combine(d, HORA_CIERRE)

    # Generación de slots cada 15 minutos (ajustable)
    step_minutes = 15

    slots = []
    t = open_dt
    delta = timedelta(minutes=duracion)
    step = timedelta(minutes=step_minutes)

    def overlaps(a_start, a_end, b_start, b_end) -> bool:
        return a_end > b_start and a_start < b_end

    while True:
        end = t + delta
        if end > close_dt:
            break

        cap = capacidad_sucursal(sucursal)
        solapados = sum(1 for x in turnos if overlaps(t, end, x.inicio, x.fin))
        if solapados < cap:
            slots.append(t.strftime("%H:%M"))

        t += step

    return jsonify({
        "ok": True,
        "duracion": duracion,
        "slots": slots
    })



@app.route("/confirmar", methods=["POST"])
def confirmar():
    nombre = (request.form.get("nombre") or "").strip()
    sucursal = (request.form.get("sucursal") or "").strip()
    direccion = (request.form.get("direccion") or "").strip()
    opcion_id = request.form.get("opcion_id")
    fecha_str = request.form.get("fecha_cita")

    if not nombre or not sucursal or not direccion or not opcion_id or not fecha_str:
        return render_template("error.html", mensaje="Faltan datos. Revisá el formulario.")

    # Parse datetime-local
    try:
        inicio = datetime.strptime(fecha_str, "%Y-%m-%dT%H:%M")
    except ValueError:
        return render_template("error.html", mensaje="Formato de fecha inválido.")

    ahora = datetime.now()

    # Regla: no turnos en el día
    if inicio.date() == ahora.date():
        return render_template("error.html", mensaje="Para turnos en el día, por favor consulta disponibilidad directamente por WhatsApp.")

    # Regla: dentro de horario (inicio)
    if not (HORA_APERTURA <= inicio.time() <= HORA_CIERRE):
        return render_template("error.html", mensaje="Horario comercial: 09:30 a 20:00hs.")

    # Opción elegida
    opcion = ServicioOpcion.query.get(int(opcion_id))
    if not opcion or not opcion.activo or not opcion.servicio.activo:
        return render_template("error.html", mensaje="La opción seleccionada no está disponible.")

    duracion = int(opcion.duracion)
    precio = int(opcion.precio)
    servicio_nombre = opcion.servicio.nombre

    fin = inicio + timedelta(minutes=duracion)

    # Regla: fin dentro de horario
    limite_fin = datetime.combine(inicio.date(), HORA_CIERRE)
    if fin > limite_fin:
        return render_template("error.html", mensaje="Ese horario termina fuera del horario de trabajo (hasta 20:00).")

    # Solapamiento por sucursal (si se pisan horarios)
    cap = capacidad_sucursal(sucursal)

    solapados = Turno.query.filter(
        Turno.sucursal == sucursal,
        Turno.fin > inicio,
        Turno.inicio < fin
    ).count()

    if solapados >= cap:
        return render_template("error.html", mensaje="Ese horario ya está completo. Elegí otro horario u otra duración.")


    # Guardar turno
    t = Turno(
        nombre=nombre,
        sucursal=sucursal,
        opcion_id=opcion.id,
        servicio_nombre=servicio_nombre,
        duracion=duracion,
        precio=precio,
        inicio=inicio,
        fin=fin
    )
    db.session.add(t)
    db.session.commit()

    # WhatsApp link con mensaje
    msg = (
        f"Hola! Soy {nombre}. Agendé {servicio_nombre} ({duracion} min) en {sucursal} "
        f"para el {inicio.strftime('%d/%m %H:%M')}. "
        f"Adjunto el comprobante de la seña de ${MONTO_SENA} enviada a {MP_TITULAR} (alias {MP_ALIAS})."
    )
    link_wa = f"https://wa.me/{WHATSAPP_NUM}?text={urllib.parse.quote(msg)}"

    return render_template(
        "confirmar.html",
        nombre=nombre,
        servicio=servicio_nombre,
        duracion=duracion,
        precio=precio,
        sucursal=sucursal,
        direccion=direccion,
        inicio=inicio,
        monto_sena=MONTO_SENA,
        alias=MP_ALIAS,
        titular=MP_TITULAR,
        link=link_wa
    )

# ======================
# ADMIN (sin login, simple)
# ======================
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = (request.form.get("password") or "").strip()

        if u == ADMIN_USER and p == ADMIN_PASS:
            session["is_admin"] = True
            return redirect("/admin")

        return render_template("admin_login.html", error="Usuario o contraseña incorrectos.")

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))



@app.route("/admin")
@admin_required
def admin_panel():
    # Filtros por querystring
    date_from = (request.args.get("from") or "").strip()   # YYYY-MM-DD
    date_to = (request.args.get("to") or "").strip()       # YYYY-MM-DD
    sucursal = (request.args.get("sucursal") or "").strip()
    servicio = (request.args.get("servicio") or "").strip()

    def parse_ymd(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None

    q = Turno.query

    df = parse_ymd(date_from)
    dt = parse_ymd(date_to)

    if df:
        q = q.filter(Turno.inicio >= df)
    if dt:
        q = q.filter(Turno.inicio < (dt + timedelta(days=1)))

    if sucursal:
        q = q.filter(Turno.sucursal == sucursal)

    if servicio:
        q = q.filter(Turno.servicio_nombre == servicio)

    turnos = q.order_by(Turno.inicio.desc()).all()

    # Listas para los selects (solo valores existentes)
    sucursales = [r[0] for r in db.session.query(Turno.sucursal).distinct().order_by(Turno.sucursal).all()]
    servicios = [r[0] for r in db.session.query(Turno.servicio_nombre).distinct().order_by(Turno.servicio_nombre).all()]

    return render_template(
        "admin_panel.html",
        turnos=turnos,
        sucursales=sucursales,
        servicios=servicios,
        filtros={
            "from": date_from,
            "to": date_to,
            "sucursal": sucursal,
            "servicio": servicio
        }
    )


@app.route("/admin/export/turnos.csv")
@admin_required
def export_turnos_csv():
    date_from = (request.args.get("from") or "").strip()
    date_to = (request.args.get("to") or "").strip()
    sucursal = (request.args.get("sucursal") or "").strip()
    servicio = (request.args.get("servicio") or "").strip()

    q = Turno.query

    def parse_ymd(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None

    df = parse_ymd(date_from)
    dt = parse_ymd(date_to)

    if df:
        q = q.filter(Turno.inicio >= df)
    if dt:
        q = q.filter(Turno.inicio < (dt + timedelta(days=1)))

    if sucursal:
        q = q.filter(Turno.sucursal == sucursal)

    if servicio:
        q = q.filter(Turno.servicio_nombre == servicio)

    turnos = q.order_by(Turno.inicio.asc()).all()

    # ... (lo demás igual)



@app.route("/admin/servicios", methods=["GET", "POST"])
@admin_required
def admin_servicios():
    if request.method == "POST":
        nombre = (request.form.get("nombre") or "").strip()
        activo = True if request.form.get("activo") else False
        if nombre:
            db.session.add(Servicio(nombre=nombre, activo=activo))
            db.session.commit()
        return redirect(url_for("admin_servicios"))

    servicios = Servicio.query.order_by(Servicio.nombre).all()
    return render_template("admin_servicios.html", servicios=servicios)

@app.route("/admin/opciones", methods=["GET", "POST"])
@admin_required
def admin_opciones():
    servicios = Servicio.query.order_by(Servicio.nombre).all()

    if request.method == "POST":
        servicio_id = request.form.get("servicio_id")
        duracion = request.form.get("duracion")
        precio = request.form.get("precio")
        activo = True if request.form.get("activo") else False

        if servicio_id and duracion and precio:
            db.session.add(
                ServicioOpcion(
                    servicio_id=int(servicio_id),
                    duracion=int(duracion),
                    precio=int(precio),
                    activo=activo
                )
            )
            db.session.commit()

        return redirect(url_for("admin_opciones"))

    opciones = ServicioOpcion.query.join(Servicio).order_by(Servicio.nombre, ServicioOpcion.duracion).all()
    return render_template("admin_opciones.html", servicios=servicios, opciones=opciones)

if __name__ == "__main__":
    app.run(debug=False, port=5001)

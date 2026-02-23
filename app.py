import os
import csv
import io
import re
import urllib.parse
from zoneinfo import ZoneInfo
from flask import session
from functools import wraps
from flask import Response
from flask import jsonify
from datetime import date as date_type
from datetime import datetime, timedelta, time
from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

print(">>> APP.PY CORRECTO CARGADO <<<")

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or os.urandom(32).hex()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

DB_PATH = os.path.join(INSTANCE_DIR, "turnos.db")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "1234")

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
    telefono = db.Column(db.String(30), nullable=False, default="")
    observacion = db.Column(db.Text, nullable=True, default="")

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
TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")

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
    {"id": 1, "nombre": "Nuñez", "direccion": "Nuñez 0000"},
    {"id": 2, "nombre": "Villa Urquiza", "direccion": "Villa Urquiza 0000"},
]

SUCURSAL_MAP = {
    1: {"nombre": "Nuñez", "direccion": "Nuñez 0000"},
    2: {"nombre": "Villa Urquiza", "direccion": "Villa Urquiza 0000"},
}

MONTO_SENA = 10000
MP_ALIAS = "Mercado.P"
MP_TITULAR = "Carmen Maria De La Concepción"
WHATSAPP_NUM = "5491123869037"


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
    
def _parse_int(raw):
    try: 
        return int(raw)
    except (TypeError, ValueError):
        return None

def ahora_local_naive():
    return datetime.now(TZ_AR).replace(tzinfo=None)

def normalizar_telefono(raw: str) -> str:
    return re.sub(r"\D+", "", raw or "")

def hay_capacidad_para_turno(sucursal: str, inicio: datetime, fin: datetime) -> bool:
    solapados = Turno.query.filter(
        Turno.sucursal == sucursal,
        Turno.fin > inicio,
        Turno.inicio < fin
    ).count()
    return solapados < capacidad_sucursal(sucursal)

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

    opcion_id_int = _parse_int(opcion_id)
    if opcion_id_int is None:
        return jsonify({"ok": False, "error": "Opción inválida"}), 400
    
    opcion = db.session.get(ServicioOpcion, opcion_id_int)
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

# ... (Aquí va tu configuración de Base de Datos y modelos) ...

@app.route('/confirmar', methods=['POST'])
def confirmar():
    # 1. Hora local de Argentina
    ahora = ahora_local_naive()
    hora_actual = ahora.hour

    # 2. Bloqueo de Madrugada (23:00 a 06:00)
    if hora_actual >= 23 or hora_actual < 6:
        mensaje = "El horario para solicitar turnos comienza a las 06:00 am. ¡Te esperamos pronto!"
        return render_template('error.html', mensaje=mensaje)

    # 3. Captura de Datos del Formulario
    nombre = (request.form.get('nombre') or "").strip()
    telefono = normalizar_telefono(request.form.get('telefono'))
    sucursal = (request.form.get('sucursal') or "").strip()
    direccion = (request.form.get('direccion') or "").strip()
    opcion_id = request.form.get('opcion_id')
    fecha_turno_str = request.form.get('fecha_cita')

    try:
        if not nombre or not telefono or not sucursal or not opcion_id or not fecha_turno_str:
            return render_template('error.html', mensaje="Faltan datos obligatorios.")
        
        fecha_turno = datetime.strptime(fecha_turno_str, '%Y-%m-%dT%H:%M')
        
        opcion_id_int = _parse_int(opcion_id)
        if opcion_id_int is None:
            return render_template('error.html', mensaje="Servicio inválido.")
    
        opcion = db.session.get(ServicioOpcion, opcion_id_int)
        if not opcion or not opcion.activo or not opcion.servicio.activo:
            return render_template('error.html', mensaje="La opción seleccionada no está disponible.")

        # Validación de disponibilidad inmediata
        if fecha_turno < ahora:
            return render_template('error.html', mensaje="No podés elegir un horario que ya pasó.")

        if fecha_turno.time() < HORA_APERTURA or fecha_turno.time() >= HORA_CIERRE:
                return render_template('error.html', mensaje="El horario elegido está fuera del horario comercial.")
    
        fin_turno = fecha_turno + timedelta(minutes=opcion.duracion)
        if fin_turno.time() > HORA_CIERRE:
            return render_template('error.html', mensaje="El turno supera el horario de cierre.")
        
        if not hay_capacidad_para_turno(sucursal, fecha_turno, fin_turno):
            return render_template('error.html', mensaje="Ese horario se ocupó recién. Elegí otro disponible.")

        # 4. GUARDAR EN BASE DE DATOS
        nuevo_turno = Turno(
            nombre=nombre,
            telefono=telefono,
            sucursal=sucursal,
            opcion_id=opcion.id,
            servicio_nombre=opcion.servicio.nombre,
            duracion=opcion.duracion,
            precio=opcion.precio,
            inicio=fecha_turno,
            fin=fin_turno
        )
        db.session.add(nuevo_turno)
        db.session.commit()

        # 5. Generar Link de WhatsApp
        texto_wa = f"Hola, soy {nombre}. Reservé {opcion.servicio.nombre} para el {fecha_turno.strftime('%d/%m a las %H:%M')} en {sucursal}."
        texto_enc = urllib.parse.quote(texto_wa)
        link_whatsapp = f"https://wa.me/{WHATSAPP_NUM}?text={texto_enc}"

        return render_template('confirmar.html', 
                             nombre=nombre, inicio=fecha_turno, sucursal=sucursal,
                             servicio=opcion.servicio.nombre, duracion=opcion.duracion,
                             precio=opcion.precio, monto_sena=MONTO_SENA,
                             alias=MP_ALIAS, titular=MP_TITULAR,
                             direccion=direccion, link=link_whatsapp)

    except Exception:
        app.logger.exception("Error al confirmar turno")
        db.session.rollback()
        return render_template('error.html', mensaje="Ocurrió un error al confirmar el turno.")

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
    filtros = {
        "from": request.args.get("from", ""),
        "to": request.args.get("to", ""),
        "sucursal": request.args.get("sucursal", ""),
        "servicio": request.args.get("servicio", ""),
        "cliente_id": request.args.get("cliente_id", ""),
    }

    q = Turno.query

    # Filtro fechas
    if filtros["from"]:
        try:
            df = datetime.strptime(filtros["from"], "%Y-%m-%d")
            q = q.filter(Turno.inicio >= df)
        except:
            pass

    if filtros["to"]:
        try:
            dt = datetime.strptime(filtros["to"], "%Y-%m-%d") + timedelta(days=1)
            q = q.filter(Turno.inicio < dt)
        except:
            pass

    # Filtro sucursal
    if filtros["sucursal"]:
        q = q.filter(Turno.sucursal == filtros["sucursal"])

    # Filtro servicio
    if filtros["servicio"]:
        q = q.filter(Turno.servicio_nombre == filtros["servicio"])

    # Filtro cliente por teléfono
    if filtros["cliente_id"]:
        cid = normalizar_telefono(filtros["cliente_id"])
        q = q.filter(Turno.telefono.contains(cid))

    turnos = q.order_by(Turno.inicio.desc()).all()

    # Conteo de visitas por teléfono
    visitas_por_tel = {}
    tels = {t.telefono for t in turnos if t.telefono}
    if tels:
        filas = (
            db.session.query(Turno.telefono, func.count(Turno.id))
            .filter(Turno.telefono.in_(list(tels)))
            .group_by(Turno.telefono)
            .all()
        )
        visitas_por_tel = {tel: cnt for tel, cnt in filas}

    # Listas para filtros
    sucursales = sorted({t.sucursal for t in Turno.query.all()})
    servicios = sorted({t.servicio_nombre for t in Turno.query.all()})

    return render_template(
        "admin_panel.html",
        turnos=turnos,
        filtros=filtros,
        visitas_por_tel=visitas_por_tel,
        sucursales=sucursales,
        servicios=servicios
    )

@app.route("/admin/export/turnos.csv")
@admin_required
def export_turnos_csv():
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    sucursal = request.args.get("sucursal", "")
    servicio = request.args.get("servicio", "")
    cliente_id = request.args.get("cliente_id", "")

    q = Turno.query

    if date_from:
        try:
            df = datetime.strptime(date_from, "%Y-%m-%d")
            q = q.filter(Turno.inicio >= df)
        except:
            pass

    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            q = q.filter(Turno.inicio < dt)
        except:
            pass

    if sucursal:
        q = q.filter(Turno.sucursal == sucursal)

    if servicio:
        q = q.filter(Turno.servicio_nombre == servicio)

    if cliente_id:
        cid = normalizar_telefono(cliente_id)
        q = q.filter(Turno.telefono.contains(cid))

    turnos = q.order_by(Turno.id.asc()).all()

    output = io.StringIO()
    output.write("\ufeff")  # BOM UTF-8 (Excel)
    writer = csv.writer(output, delimiter=";")

    # Orden EXACTO que pediste
    writer.writerow([
        "ID Turno",
        "Inicio",
        "Cliente",
        "Teléfono",
        "Sucursal",
        "Servicio",
        "Visitas",
        "Duración (min)",
        "Precio",
        "Observación"
    ])

    # Visitas globales (por teléfono) para los teléfonos presentes en el reporte
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
        writer.writerow([
            t.id,
            t.inicio.strftime("%d/%m/%Y %H:%M"),
            t.nombre,
            t.telefono,
            t.sucursal,
            t.servicio_nombre,
            visitas_por_tel.get(t.telefono, 1),
            t.duracion,
            t.precio,
            (t.observacion or "")
        ])

    csv_data = output.getvalue()
    output.close()

    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=turnos.csv"}
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

    csv_data = output.getvalue()
    output.close()

    return Response(csv_data, mimetype="text/csv; charset=utf-8",
    headers={"Content-Disposition": "attachment; filename=resumen_sucursal.csv"})

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

    csv_data = output.getvalue()
    output.close()

    return Response(csv_data, mimetype="text/csv; charset=utf-8",
    headers={"Content-Disposition": "attachment; filename=resumen_servicio.csv"})

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

    # orden por total desc
    for tel, data in sorted(resumen.items(), key=lambda x: x[1]["total"], reverse=True):
        writer.writerow([tel, data["nombre"], data["cantidad"], data["total"]])

    csv_data = output.getvalue()
    output.close()

    return Response(csv_data, mimetype="text/csv; charset=utf-8",
    headers={"Content-Disposition": "attachment; filename=resumen_clientes.csv"})



@app.route("/admin/servicios", methods=["GET", "POST"])
@admin_required
def admin_servicios():
    if request.method == "POST":
        form_type = (request.form.get("form_type") or "servicio").strip()

        if form_type == "opcion":
            servicio_id = _parse_int(request.form.get("servicio_id"))
            duracion = _parse_int(request.form.get("duracion"))
            precio = _parse_int(request.form.get("precio"))
            activo = True if request.form.get("activo") else False

            servicio = db.session.get(Servicio, servicio_id) if servicio_id else None
            if servicio and duracion and precio and duracion > 0 and precio >= 0:
                db.session.add(
                    ServicioOpcion(
                        servicio_id=servicio.id,
                        duracion=duracion,
                        precio=precio,
                        activo=activo
                    )
                )
                db.session.commit()
        else:
            nombre = (request.form.get("nombre") or "").strip()
            activo = True if request.form.get("activo") else False
            if nombre:
                db.session.add(Servicio(nombre=nombre, activo=activo))
                db.session.commit()
        return redirect(url_for("admin_servicios"))

    servicios = Servicio.query.order_by(Servicio.nombre).all()
    opciones = (
        ServicioOpcion.query.join(Servicio)
        .order_by(Servicio.nombre, ServicioOpcion.duracion)
        .all()
    )
    return render_template("admin_servicios.html", servicios=servicios, opciones=opciones)

@app.route("/admin/opciones", methods=["GET", "POST"])
@admin_required
def admin_opciones():
    if request.method == "GET":
        return redirect(url_for("admin_servicios"))

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

        return redirect(url_for("admin_servicios"))

    opciones = ServicioOpcion.query.join(Servicio).order_by(Servicio.nombre, ServicioOpcion.duracion).all()
    return render_template("admin_opciones.html", servicios=servicios, opciones=opciones)

@app.route("/admin/turno/<int:turno_id>/observacion", methods=["POST"])
@admin_required
def admin_guardar_observacion(turno_id: int):
    t = Turno.query.get_or_404(turno_id)
    obs = (request.form.get("observacion") or "").strip()
    t.observacion = obs
    db.session.commit()
    return redirect(url_for("admin_panel"))

if __name__ == "__main__":
    app.run(debug=False, port=5001)

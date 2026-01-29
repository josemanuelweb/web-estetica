from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = 'ifts12_secreto_profesional_estetica'

basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'turnos.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

class Turno(db.Model):
    __tablename__ = 'turno'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    servicio = db.Column(db.String(100), nullable=False)
    fecha_cita = db.Column(db.DateTime, nullable=False)
    fecha_registro = db.Column(db.DateTime, default=datetime.now)

with app.app_context():
    db.create_all()

@app.route('/')
def index():
    servicios = [
        {'nombre': 'Masaje Descontracturante', 'precio': 36000},
        {'nombre': 'Limpieza Facial Profunda', 'precio': 35000},
        {'nombre': 'Trilogía Alivio Profundo', 'precio': 99000},
        {'nombre': 'Relax para Dos', 'precio': 100000}
    ]
    return render_template('index.html', servicios=servicios)

@app.route('/turnos', methods=['GET', 'POST'])
def turnos():
    if request.method == 'POST':
        nombre = request.form.get('nombre')
        email = request.form.get('email')
        servicio = request.form.get('servicio')
        fecha_str = request.form.get('fecha_cita')
        
        try:
            fecha_dt = datetime.strptime(fecha_str, '%Y-%m-%dT%H:%M')
        except:
            return "Error: Fecha inválida."

        if fecha_dt.weekday() == 6:
            return "Error: No abrimos los domingos."
        if fecha_dt.hour < 9 or fecha_dt.hour >= 19:
            return "Error: Horario de 9 a 19hs."

        nuevo_turno = Turno(nombre=nombre, email=email, servicio=servicio, fecha_cita=fecha_dt)
        db.session.add(nuevo_turno)
        db.session.commit()

        tel = "54911XXXXXXXX" 
        msg = f"Hola! Soy {nombre}, reservé para {servicio} el día {fecha_str.replace('T', ' ')}."
        link_wa = f"https://wa.me/{tel}?text={msg.replace(' ', '%20')}"
        
        return render_template('confirmacion.html', nombre=nombre, link=link_wa)
        
    return render_template('turnos.html')

@app.route('/admin')
def admin():
    # Obtenemos la clave de la dirección (ej: /admin?clave=1234)
    password = request.args.get('password')
    
    # Definí una clave segura para el dueño
    if password != "ifts12_estetica":
        return "Acceso denegado. No tenés permisos para ver esta página.", 403

    ahora = datetime.now()
    Turno.query.filter(Turno.fecha_cita < ahora).delete()
    db.session.commit()
    turnos_pendientes = Turno.query.order_by(Turno.fecha_cita.asc()).all()
    return render_template('admin.html', turnos=turnos_pendientes)

# --- ESTA ES LA FUNCIÓN QUE FALTABA Y CAUSABA EL ERROR ---
@app.route('/finalizar/<int:id>')
def finalizar(id):
    turno = Turno.query.get_or_404(id)
    db.session.delete(turno)
    db.session.commit()
    return redirect(url_for('admin'))

@app.route('/api/turnos')
def api_turnos():
    turnos = Turno.query.all()
    return {"turnos": [{"id": t.id} for t in turnos]}

if __name__ == '__main__':
    app.run(debug=True, port=5001)
import os
from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

app = Flask(__name__)
app.secret_key = 'entrega_final_estetica'

# Configuración de Base de Datos
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'turnos.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Modelo simplificado (Email opcional para que no falle)
class Turno(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    servicio = db.Column(db.String(100), nullable=False)
    fecha_cita = db.Column(db.DateTime, nullable=False)

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
        servicio = request.form.get('servicio')
        fecha_str = request.form.get('fecha_cita')
        
        try:
            fecha_dt = datetime.strptime(fecha_str, '%Y-%m-%dT%H:%M')
            nuevo_turno = Turno(nombre=nombre, servicio=servicio, fecha_cita=fecha_dt)
            db.session.add(nuevo_turno)
            db.session.commit()
            
            # Número del dueño (Cambiá las X por el número real)
            numero_wa = "5491123869037" 
            mensaje = f"Hola! Soy {nombre}. Reservé: {servicio} para el {fecha_dt.strftime('%d/%m')} a las {fecha_dt.strftime('%H:%M')}hs."
            link_wa = f"https://wa.me/{numero_wa}?text={mensaje.replace(' ', '%20')}"
            
            return render_template('confirmacion.html', nombre=nombre, link=link_wa)
        except Exception:
            db.session.rollback()
            return redirect(url_for('index'))
            
    return render_template('turnos.html')

if __name__ == '__main__':
    app.run(debug=True, port=5001)
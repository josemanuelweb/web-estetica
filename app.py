import os
from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = 'logica_digital_2026_pro'

# 1. BASE DE DATOS
basedir = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(basedir, 'agenda.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Turno(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    servicio = db.Column(db.String(100), nullable=False)
    sucursal = db.Column(db.String(50), nullable=False)
    fecha_cita = db.Column(db.DateTime, nullable=False)

with app.app_context():
    db.create_all()

# 2. RUTAS
@app.route('/')
def index():
    sucursales = [
        {'id': 1, 'nombre': 'Barrio nuñez', 'direccion': 'Montañeses 2830 1b, CABA'},
        {'id': 2, 'nombre': 'Barrio villa urquiza', 'direccion': 'Monroe 5674 12b'}
    ]
    return render_template('index.html', sucursales=sucursales)

@app.route('/sucursal/<int:id>')
def sucursal_detalle(id):
    nombres_sucursales = {1: "Nuñez", 2: "Villa Urquiza"}
    nombre_suc = nombres_sucursales.get(id, "Sede Desconocida")
    return render_template('reservar.html', sucursal=nombre_suc)

@app.route('/confirmar', methods=['POST'])
def confirmar():
    nombre = request.form.get('nombre')
    servicio = request.form.get('servicio')
    sucursal = request.form.get('sucursal')
    fecha_str = request.form.get('fecha_cita')

    try:
        fecha_dt = datetime.strptime(fecha_str, '%Y-%m-%dT%H:%M')
        ahora = datetime.now()

        # VALIDACIONES
        if fecha_dt.date() <= ahora.date():
            return render_template('error.html', mensaje="Las reservas deben realizarse con al menos un día de anticipación.")

        hora_decimal = fecha_dt.hour + (fecha_dt.minute / 60)
        if hora_decimal < 9.5 or hora_decimal > 20:
            return render_template('error.html', mensaje="Horario comercial: 09:30 a 20:00hs.")

        inicio_rango = fecha_dt - timedelta(minutes=89)
        fin_rango = fecha_dt + timedelta(minutes=89)
        simultaneos = Turno.query.filter(Turno.sucursal == sucursal, Turno.fecha_cita.between(inicio_rango, fin_rango)).count()

        if simultaneos >= 2:
            return render_template('error.html', mensaje="No hay masajistas disponibles en este horario.")

        # GUARDAR
        nuevo_turno = Turno(nombre=nombre, servicio=servicio, sucursal=sucursal, fecha_cita=fecha_dt)
        db.session.add(nuevo_turno)
        db.session.commit()

        # DATOS DE PAGO Y WHATSAPP
        alias = "promasaje"
        titular = "Irene Blasina Martínez Peña"
        monto_sena = "10.000"
        
        mensaje_wa = f"Hola! Soy {nombre}. Agendé {servicio} en {sucursal} para el {fecha_dt.strftime('%d/%m %H:%M')}. Aquí adjunto el comprobante de la seña de ${monto_sena} enviada a {titular}."
        link_wa = f"https://wa.me/5491151023354?text={mensaje_wa.replace(' ', '%20')}"

        # PANTALLA DE ÉXITO CON DATOS DE MERCADO PAGO
        return f"""
        <div style="font-family: 'Poppins', sans-serif; text-align: center; margin-top: 30px; color: #8e735b; padding: 20px;">
            <div style="background: white; padding: 30px; border-radius: 15px; display: inline-block; box-shadow: 0 4px 12px rgba(0,0,0,0.05); max-width: 400px;">
                <h1 style="color: #28a745; margin-bottom: 10px;">¡Casi listo!</h1>
                <p>Para confirmar tu turno, por favor realizá la seña:</p>
                
                <div style="background: #fdf6f6; padding: 20px; border-radius: 10px; margin: 20px 0; border: 1px solid #d4a373; text-align: left;">
                    <p style="margin: 5px 0;"><strong>Monto:</strong> ${monto_sena}</p>
                    <p style="margin: 5px 0;"><strong>Alias MP:</strong> <span style="color: #009ee3;">{alias}</span></p>
                    <p style="margin: 5px 0; font-size: 0.9em;"><strong>Titular:</strong> {titular}</p>
                </div>

                <p style="font-size: 0.9em;">Una vez hecho el pago, hacé clic abajo para <strong>enviar el comprobante</strong>:</p>
                
                <a href="{link_wa}" style="background-color: #d4a373; color: white; padding: 15px 25px; text-decoration: none; border-radius: 25px; font-weight: bold; display: inline-block; margin-top: 10px;">
                    ENVIAR COMPROBANTE POR WHATSAPP
                </a>
            </div>
        </div>
        """

    except Exception as e:
        db.session.rollback()
        return render_template('error.html', mensaje=f"Error: {e}")

if __name__ == '__main__':
    app.run(debug=True, port=5001)
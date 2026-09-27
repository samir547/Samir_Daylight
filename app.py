from flask import Flask
from flask_sqlalchemy import SQLAlchemy
import os

db = SQLAlchemy()

def create_app():
    app = Flask(__name__)
    base_dir = os.path.abspath(os.path.dirname(__file__))
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(base_dir, 'db', 'appointments.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = 'workshop-secret-key'

    db.init_app(app)

    from routes.appointments import appointments_bp
    from routes.doctors import doctors_bp
    app.register_blueprint(appointments_bp)
    app.register_blueprint(doctors_bp)

    @app.route('/health')
    def health():
        return {'status': 'ok', 'service': 'patient-appointment-scheduler'}

    with app.app_context():
        db.create_all()

    return app

app = create_app()

if __name__ == '__main__':
    app.run(debug=True)

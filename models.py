from app import db
from datetime import datetime

class Doctor(db.Model):
    __tablename__ = 'doctors'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    department = db.Column(db.String(100), nullable=False)
    appointments = db.relationship('Appointment', backref='doctor')

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'department': self.department}

class Patient(db.Model):
    __tablename__ = 'patients'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    dob = db.Column(db.String(20), nullable=False)
    nhs_number = db.Column(db.String(20), unique=True, nullable=False)
    appointments = db.relationship('Appointment', backref='patient', lazy=True)

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'dob': self.dob, 'nhs_number': self.nhs_number}

class Appointment(db.Model):
    __tablename__ = 'appointments'
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey('patients.id'), nullable=False)
    doctor_id = db.Column(db.Integer, db.ForeignKey('doctors.id'), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    reason = db.Column(db.String(200))
    status = db.Column(db.String(20), default='scheduled')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'patient_id': self.patient_id,
            'doctor_id': self.doctor_id,
            'start_time': self.start_time.isoformat(),
            'end_time': self.end_time.isoformat(),
            'reason': self.reason,
            'status': self.status
        }

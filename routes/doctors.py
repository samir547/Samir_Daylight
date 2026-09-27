from flask import Blueprint, request, jsonify
from app import db
from models import Doctor, Appointment
from datetime import datetime

doctors_bp = Blueprint('doctors', __name__)

@doctors_bp.route('/doctors', methods=['GET'])
def get_doctors():
    doctors = Doctor.query.all()
    return jsonify({'data': [d.to_dict() for d in doctors], 'error': None, 'status': 200})

@doctors_bp.route('/doctors/<int:doctor_id>', methods=['GET'])
def get_doctor(doctor_id):
    doctor = Doctor.query.get(doctor_id)
    if not doctor:
        return jsonify({'data': None, 'error': 'Doctor not found', 'status': 404}), 404
    return jsonify({'data': doctor.to_dict(), 'error': None, 'status': 200})

@doctors_bp.route('/doctors/<int:doctor_id>/slots', methods=['GET'])
def get_doctor_slots(doctor_id):
    doctor = Doctor.query.get(doctor_id)
    if not doctor:
        return jsonify({'data': None, 'error': 'Doctor not found', 'status': 404}), 404
    date_str = request.args.get('date')
    query = Appointment.query.filter_by(doctor_id=doctor_id, status='scheduled')
    if date_str:
        try:
            date = datetime.strptime(date_str, '%Y-%m-%d')
            query = query.filter(db.func.date(Appointment.start_time) == date.date())
        except ValueError:
            return jsonify({'data': None, 'error': 'Invalid date format. Use YYYY-MM-DD.', 'status': 400}), 400
    appointments = query.all()
    return jsonify({
        'data': {'doctor': doctor.to_dict(), 'booked_slots': [a.to_dict() for a in appointments]},
        'error': None, 'status': 200
    })

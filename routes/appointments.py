from flask import Blueprint, request, jsonify
from app import db
from models import Appointment, Patient
from utils.conflict import check_overlap
from datetime import datetime
from sqlalchemy.orm import joinedload

appointments_bp = Blueprint('appointments', __name__)

@appointments_bp.route('/appointments', methods=['GET'])
def get_appointments():
    patient_id = request.args.get('patient_id', type=int)
    doctor_id = request.args.get('doctor_id', type=int)
    query = Appointment.query.options(joinedload(Appointment.doctor))
    if patient_id:
        query = query.filter_by(patient_id=patient_id)
    if doctor_id:
        query = query.filter_by(doctor_id=doctor_id)
    appointments = query.all()
    result = []
    for a in appointments:
        data = a.to_dict()
        data['doctor_name'] = a.doctor.name if a.doctor else None
        result.append(data)
    return jsonify({'data': result, 'error': None, 'status': 200})

@appointments_bp.route('/appointments/<int:appt_id>', methods=['GET'])
def get_appointment(appt_id):
    appt = Appointment.query.get(appt_id)
    if not appt:
        return jsonify({'data': None, 'error': 'Appointment not found', 'status': 404}), 404
    return jsonify({'data': appt.to_dict(), 'error': None, 'status': 200})

@appointments_bp.route('/appointments', methods=['POST'])
def create_appointment():
    data = request.get_json()
    if not data:
        return jsonify({'data': None, 'error': 'No data provided', 'status': 400}), 400
    required = ['patient_id', 'doctor_id', 'start_time', 'end_time']
    for field in required:
        if field not in data:
            return jsonify({'data': None, 'error': f'Missing field: {field}', 'status': 400}), 400
    try:
        start = datetime.fromisoformat(data['start_time'])
        end = datetime.fromisoformat(data['end_time'])
    except ValueError:
        return jsonify({'data': None, 'error': 'Invalid datetime format. Use ISO 8601.', 'status': 400}), 400
    if end <= start:
        return jsonify({'data': None, 'error': 'end_time must be after start_time', 'status': 400}), 400
    if check_overlap(data['doctor_id'], start, end):
        return jsonify({'data': None, 'error': 'Time slot conflicts with existing appointment', 'status': 409}), 409
    appt = Appointment(
        patient_id=data['patient_id'],
        doctor_id=data['doctor_id'],
        start_time=start,
        end_time=end,
        reason=data.get('reason', ''),
        status='scheduled'
    )
    db.session.add(appt)
    db.session.commit()
    return jsonify({'data': appt.to_dict(), 'error': None, 'status': 201}), 201

@appointments_bp.route('/appointments/<int:appt_id>', methods=['PUT'])
def reschedule_appointment(appt_id):
    appt = Appointment.query.get(appt_id)
    if not appt:
        return jsonify({'data': None, 'error': 'Appointment not found', 'status': 404}), 404
    data = request.get_json()
    try:
        new_start = datetime.fromisoformat(data['start_time'])
        new_end = datetime.fromisoformat(data['end_time'])
    except (ValueError, KeyError):
        return jsonify({'data': None, 'error': 'Invalid or missing datetime fields', 'status': 400}), 400
    if check_overlap(appt.doctor_id, new_start, new_end, exclude_id=appt_id):
        return jsonify({'data': None, 'error': 'New time slot conflicts with existing appointment', 'status': 409}), 409
    appt.start_time = new_start
    appt.end_time = new_end
    db.session.commit()
    return jsonify({'data': appt.to_dict(), 'error': None, 'status': 200})

@appointments_bp.route('/appointments/<int:appt_id>', methods=['DELETE'])
def cancel_appointment(appt_id):
    appt = Appointment.query.get(appt_id)
    if not appt:
        return jsonify({'data': None, 'error': 'Appointment not found', 'status': 404}), 404
    appt.status = 'cancelled'
    db.session.commit()
    return jsonify({'data': {'id': appt_id, 'status': 'cancelled'}, 'error': None, 'status': 200})

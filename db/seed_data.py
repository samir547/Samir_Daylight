"""
Seed the database with sample data for the workshop.
Run from the project root: python db/seed_data.py
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app, db
from models import Doctor, Patient, Appointment
from datetime import datetime, timedelta

def seed():
    app = create_app()
    with app.app_context():
        db.drop_all()
        db.create_all()

        doctors = [
            Doctor(name='Dr. Sarah Chen',     department='Cardiology'),
            Doctor(name='Dr. James Okafor',   department='Neurology'),
            Doctor(name='Dr. Priya Sharma',   department='Oncology'),
            Doctor(name='Dr. Mark Davies',    department='Orthopaedics'),
            Doctor(name='Dr. Aisha Patel',    department='General Practice'),
        ]
        db.session.add_all(doctors)
        db.session.commit()

        patients = [
            Patient(name='Alice Thompson',  dob='1985-03-12', nhs_number='NHS-001'),
            Patient(name='Bob Martins',     dob='1972-07-24', nhs_number='NHS-002'),
            Patient(name='Carol White',     dob='1990-11-05', nhs_number='NHS-003'),
            Patient(name='David Kumar',     dob='1965-01-30', nhs_number='NHS-004'),
            Patient(name='Emma Johnson',    dob='2001-08-19', nhs_number='NHS-005'),
            Patient(name='Frank Osei',      dob='1978-04-14', nhs_number='NHS-006'),
            Patient(name='Grace Li',        dob='1995-09-22', nhs_number='NHS-007'),
            Patient(name='Henry Patel',     dob='1960-12-01', nhs_number='NHS-008'),
            Patient(name='Isabel Cruz',     dob='1988-06-17', nhs_number='NHS-009'),
            Patient(name='James Wright',    dob='2005-02-28', nhs_number='NHS-010'),
        ]
        db.session.add_all(patients)
        db.session.commit()

        base = datetime(2025, 7, 1, 9, 0)
        appts = []
        slot_pairs = [
            (0, 0), (0, 1), (1, 0), (1, 2),
            (2, 1), (2, 3), (3, 0), (3, 4),
            (4, 2), (4, 3), (0, 4), (1, 5),
            (2, 6), (3, 7), (4, 8), (0, 9),
            (1, 6), (2, 7), (3, 8), (4, 9),
        ]
        reasons = [
            'Annual check-up', 'Follow-up consultation', 'Blood pressure review',
            'ECG analysis', 'MRI results', 'Physiotherapy referral',
            'Medication review', 'Post-surgery check', 'New patient intake', 'Prescription renewal',
        ]
        for i, (doc_idx, pat_idx) in enumerate(slot_pairs):
            day_offset = i // 4
            hour_offset = (i % 4) * 1
            start = base + timedelta(days=day_offset, hours=hour_offset)
            appts.append(Appointment(
                patient_id=patients[pat_idx].id,
                doctor_id=doctors[doc_idx].id,
                start_time=start,
                end_time=start + timedelta(minutes=30),
                reason=reasons[i % len(reasons)],
                status='scheduled'
            ))
        db.session.add_all(appts)
        db.session.commit()
        print(f"Seeded: {len(doctors)} doctors, {len(patients)} patients, {len(appts)} appointments")

if __name__ == '__main__':
    seed()

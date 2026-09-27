from models import Appointment

def check_overlap(doctor_id, start_time, end_time, exclude_id=None):
    """
    Check whether a proposed appointment slot overlaps with any existing
    scheduled appointments for a given doctor.

    Two appointments overlap when one starts before the other ends AND ends
    after the other starts. This uses strict less-than comparisons so that
    back-to-back appointments (e.g. 09:00-09:30 followed by 09:30-10:00)
    are correctly allowed.

    Args:
        doctor_id:   ID of the doctor whose schedule to check
        start_time:  Proposed appointment start (datetime)
        end_time:    Proposed appointment end (datetime)
        exclude_id:  Optional appointment ID to ignore (used during reschedule)

    Returns:
        True if an overlap exists, False if the slot is free
    """
    query = Appointment.query.filter(
        Appointment.doctor_id == doctor_id,
        Appointment.status == 'scheduled',
        Appointment.start_time < end_time,
        Appointment.end_time > start_time,
    )
    if exclude_id:
        query = query.filter(Appointment.id != exclude_id)
    return query.first() is not None


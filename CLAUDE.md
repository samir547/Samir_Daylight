# Patient Appointment Scheduler (Workshop Debug Version)

## Project Overview
Patient appointment scheduling REST API for NHS clinic admin staff.
This version contains known issues introduced for the M6 bug hunting lab.
Your task: find and fix all bugs using Claude Code.

## Tech Stack
- Flask 3.0 · SQLAlchemy · SQLite · Python 3.11
- Entry point: app.py
- Key files: routes/appointments.py, utils/conflict.py, models.py

## Coding Conventions
- snake_case for all variables and functions
- Routes return JSON: {data, error, status}
- No inline SQL — SQLAlchemy ORM only
- HTTP codes: 200 success, 201 created, 400 bad request, 404 not found, 409 conflict

## Do Not Touch
- Do not modify /health endpoint
- Do not change the JSON response shape

## Known Issues (for instructor reference only)
This file is the participant version — bugs are NOT documented here.
Use the M6 Debug Guide for symptom descriptions and fix verification.

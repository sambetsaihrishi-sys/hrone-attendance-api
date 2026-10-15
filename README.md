# HROne Attendance Management API

A FastAPI-based employee attendance management system using MongoDB. The application supports employee management, attendance tracking, corrections, analytics, and MongoDB query explain plans.

## Tech Stack

- Python 3.11
- FastAPI
- MongoDB Atlas
- PyMongo
- Uvicorn

## Features

- Employee creation and listing
- Employee punch-in and punch-out
- Attendance records with filtering
- Attendance corrections
- Monthly employee attendance analytics
- Department attendance summaries
- Late arrival leaderboard
- Department attendance trends
- MongoDB query execution plans for five supported endpoints

## Setup

### 1. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Configure MongoDB

Create a `.env` file in the project root:

```env
MONGO_URI=your_mongodb_connection_string
MONGO_DB=hrone_attendance
```

Use MongoDB 6.0 or newer.

**Never commit `.env` or database credentials to GitHub.**

### 4. Seed sample data

```powershell
python sample_seed.py
```

### 5. Start the API

```powershell
uvicorn app.main:app --port 8000
```

API documentation:

http://127.0.0.1:8000/docs

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| POST | `/employees` | Create employee |
| GET | `/employees` | List employees |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | List attendance records |
| PATCH | `/attendance/{attendance_id}` | Correct attendance |
| GET | `/analytics/employee/{emp_code}/monthly` | Monthly employee analytics |
| GET | `/analytics/department/summary` | Department attendance summary |
| GET | `/analytics/late-leaderboard` | Late arrival leaderboard |
| GET | `/analytics/department/trend` | Department attendance trend |
| GET | `/admin/explain/{endpoint}` | MongoDB query execution plan |

## MongoDB Query Explain

The `/admin/explain/{endpoint}` endpoint supports:

- `attendance_list`
- `employee_monthly`
- `department_summary`
- `late_leaderboard`
- `department_trend`

Explain responses use MongoDB execution statistics to help inspect query plans and index usage.

## Project Structure

```text
candidate_kit/
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
├── sample_seed.py
├── requirements.txt
├── README.md
├── REVIEW.md
├── DECISIONS.md
├── DATA_MODEL.md
└── openapi.yaml
```

## Additional Documentation

- `REVIEW.md` — implementation review and testing notes
- `DECISIONS.md` — technical decisions and trade-offs
- `DATA_MODEL.md` — MongoDB data model
- `openapi.yaml` — API contract

## Security

Database credentials are stored in environment variables. The `.env` file and Python virtual environment are excluded from Git using `.gitignore`.
# Employee Attendance & Analytics API

A FastAPI + MongoDB implementation of the HROne Software Engineer Trainee assignment.

## Features

- **Employee Management**: Create and list employees with unique `emp_code`
- **Attendance Tracking**: Punch-in/punch-out with business rules (R1-R10)
- **Manual Corrections**: Regularize attendance records with full audit trail (`history`)
- **Analytics**: Monthly summaries, department summaries, late leaderboard, daily trends
- **Admin**: MongoDB `explain` endpoint for query plan inspection

## Business Rules Implemented

| Rule | Description |
|------|-------------|
| R1 | IST timezone; attendance date from punch-in; overnight shift handling |
| R2 | Late minutes: >10 min grace, floor minutes since shift start |
| R3 | Overtime minutes: floor minutes past shift end, only if ≥30 |
| R4 | Work hours: (punch_out - punch_in) / 3600, 2dp half-up rounding |
| R5 | Half day: rounded work_hours < 4.50 |
| R6 | Presence: PRESENT, WFH, ON_DUTY count; ABSENT, LEAVE don't |
| R7 | Working days: Mon-Fri from `joined_on` |
| R8 | Reported numbers: 2dp half-up; rates: 4dp half-up |
| R9 | Headcount: employees with `joined_on` ≤ period end |
| R10 | Pagination: page starts at 1, page_size max 100 |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Readiness probe (MongoDB ping) |
| POST | `/employees` | Create employee |
| GET | `/employees` | List employees (paginated, filter by department) |
| POST | `/attendance/punch-in` | Record punch-in |
| POST | `/attendance/punch-out` | Record punch-out |
| GET | `/attendance` | List attendance records (paginated, filtered) |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize (correct) a record |
| GET | `/analytics/employees/{emp_code}/monthly` | Employee monthly summary |
| GET | `/analytics/departments/summary` | Department monthly summary |
| GET | `/analytics/leaderboard/late` | Late-comers leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily attendance trend |
| GET | `/admin/explain/{endpoint}` | MongoDB explain for endpoint query |

## Quick Start

```bash
# 1. Clone and enter directory
git clone <your-repo-url>
cd candidate_kit

# 2. Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start MongoDB (Docker)
docker run -d -p 27017:27017 --name mongodb mongo:7

# 5. Configure environment
cp .env.example .env  # Edit MONGO_URI / MONGO_DB if needed

# 6. Seed sample data
python sample_seed.py

# 7. Run API
uvicorn app.main:app --port 8000 --reload
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Database name |

## Testing

```bash
# Health check
curl http://localhost:8000/health

# Create employee
curl -X POST http://localhost:8000/employees \
  -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0007","name":"Test","email":"test@co.com","department":"Eng","joined_on":"2026-01-01"}'

# Punch in/out (epoch milliseconds)
curl -X POST http://localhost:8000/attendance/punch-in \
  -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0001"}'

# Analytics
curl "http://localhost:8000/analytics/employees/EMP0001/monthly?month=2026-07"
```

## Project Structure

```
candidate_kit/
├── app/
│   ├── __init__.py
│   └── main.py          # All application code (FastAPI, MongoDB, business logic)
├── sample_data/
│   ├── employees.json   # Sample employees (MongoDB Extended JSON)
│   └── attendance_logs.json
├── sample_seed.py       # Loads sample data into MongoDB
├── requirements.txt     # Python dependencies
├── openapi.yaml         # API contract (source of truth)
├── DATA_MODEL.md        # MongoDB schema documentation
├── REVIEW.md            # Code review of starter implementation
├── DECISIONS.md         # Design decisions and rationale
└── README.md            # This file
```

## Indexes

Created at startup (idempotent):
- `employees.emp_code` (unique)
- `employees(department, joined_on)`
- `employees.joined_on`
- `attendance_logs(emp_code, date)` (unique)
- `attendance_logs(date DESC, emp_code ASC)`
- `attendance_logs(emp_code ASC, punch_in DESC)`

## Notes

- All code in `app/main.py` per assignment requirement
- No `.env`, secrets, or Dockerfile in repository
- MongoDB 6.0+ required (uses `$densify`, `$rank`, `$setWindowFields`)
- Derived fields (`work_hours`, `late_minutes`, `overtime_minutes`, `half_day`) computed at write time; analytics read stored values
- Audit trail (`history`) appended only on manual corrections (PATCH), never on punch-in/out

## License

Submission for HROne Software Engineer Trainee assignment.
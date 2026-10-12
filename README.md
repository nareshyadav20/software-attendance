# Employee Attendance & Analytics API

FastAPI backend for attendance logging, regularization audit trail, and MongoDB aggregation analytics. Built with Python 3.11+, FastAPI, and MongoDB Atlas / PyMongo.

## Features
* Complete implementation of attendance punch-in and punch-out adhering strictly to business rules R1 - R10.
* Atomic operations and unique indexes preventing punch races and inconsistent attendance state transitions.
* Manual attendance regularization (`PATCH`) with immutable audit trail (`history`) capturing full field diffs.
* High-performance MongoDB aggregation pipelines for monthly summaries, department overviews, late leaderboard with competition ranking, and 7-day trend moving averages (`$densify`, `$setWindowFields`).
* `/admin/explain/{endpoint}` query planner diagnostic endpoint verifying index utilization (`IXSCAN`).

## Requirements
* Python 3.11+
* MongoDB 6.0+ (MongoDB Atlas or local MongoDB instance)

## Setup & Local Installation

1. **Clone the repository**:
   ```bash
   git clone <repo-url>
   cd attendance-software
   ```

2. **Create and activate a virtual environment**:
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # macOS/Linux:
   source .venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**:
   Copy `.env.example` to `.env` and configure your MongoDB connection:
   ```bash
   cp .env.example .env
   ```
   Example configuration:
   ```env
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=hrone_attendance
   ```
   *(For MongoDB Atlas, supply your Atlas URI in `MONGO_URI`)*

5. **Seed sample data**:
   ```bash
   python sample_seed.py
   ```

6. **Start the API service**:
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```
   Interactive API documentation is available at `http://localhost:8000/docs`.

## Running Tests
Run the comprehensive test suite with `pytest`:
```bash
pytest -v
```

## Review & Technical Decisions
* [REVIEW.md](file:///d:/attendance%20-software/REVIEW.md): Detailed code review of starter implementation defects and fixes.
* [DECISIONS.md](file:///d:/attendance%20-software/DECISIONS.md): Architectural decisions covering indexes, race handling, ties, headcount, and horizontal scalability.

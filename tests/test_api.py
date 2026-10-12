"""
Unit and integration tests for HROne Employee Attendance & Analytics API.
Covers core lifecycle, concurrency semantics, edge cases (ties, mid-month joiners,
no-log employees, weekend records, overnight shifts), and explain plans.
"""
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from app.main import (
    app,
    db,
    round_half_up,
    compute_late_minutes,
    compute_overtime_minutes,
    compute_work_hours,
    compute_half_day,
    determine_attendance_date,
    get_shift_instants,
    count_working_days,
    IST,
)

test_client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    from app.main import init_indexes
    init_indexes()


def test_health():
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_rounding_half_up():
    assert round_half_up(2.345, 2) == 2.35
    assert round_half_up(2.344, 2) == 2.34
    assert round_half_up(2.3451, 2) == 2.35
    assert round_half_up(None, 2) is None


def test_business_rules_late_grace():
    # Shift 09:30 IST on 2026-07-06
    shift_start, _ = get_shift_instants("2026-07-06", "09:30", "18:30")
    
    # 09:40:00 -> 0 (grace 10:00)
    p1 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(p1, shift_start) == 0

    # 09:40:01 -> 10 (strictly > 10:00)
    p2 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(p2, shift_start) == 10

    # 10:15:59 -> 45
    p3 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(p3, shift_start) == 45

    # Early punch-in (09:15) -> 0
    p4 = datetime(2026, 7, 6, 9, 15, 0, tzinfo=IST)
    assert compute_late_minutes(p4, shift_start) == 0


def test_business_rules_overtime():
    _, shift_end = get_shift_instants("2026-07-06", "09:30", "18:30")

    # 18:35 -> 5 min overtime -> 0 (under 30 min minimum)
    out1 = datetime(2026, 7, 6, 18, 35, 0, tzinfo=IST)
    assert compute_overtime_minutes(out1, shift_end) == 0

    # 18:59 -> 29 min overtime -> 0
    out2 = datetime(2026, 7, 6, 18, 59, 0, tzinfo=IST)
    assert compute_overtime_minutes(out2, shift_end) == 0

    # 19:00 -> 30 min overtime -> 30
    out3 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
    assert compute_overtime_minutes(out3, shift_end) == 30

    # 19:10 -> 40 min overtime -> 40
    out4 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST)
    assert compute_overtime_minutes(out4, shift_end) == 40


def test_work_hours_and_half_day():
    p_in = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
    # 4 hours worked -> 4.0 hours -> half_day = True (< 4.50)
    p_out1 = datetime(2026, 7, 6, 13, 30, 0, tzinfo=IST)
    wh1 = compute_work_hours(p_in, p_out1)
    assert wh1 == 4.0
    assert compute_half_day(wh1) is True

    # 4.5 hours worked -> 4.5 hours -> half_day = False
    p_out2 = datetime(2026, 7, 6, 14, 0, 0, tzinfo=IST)
    wh2 = compute_work_hours(p_in, p_out2)
    assert wh2 == 4.5
    assert compute_half_day(wh2) is False


def test_overnight_shift_date():
    # Overnight shift: 22:00 to 06:00
    # Punch in at 05:30 IST on July 7 -> belongs to July 6
    p_in = datetime(2026, 7, 7, 5, 30, 0, tzinfo=IST)
    att_date, is_overnight = determine_attendance_date(p_in, "22:00", "06:00")
    assert is_overnight is True
    assert att_date == "2026-07-06"

    # Punch in at 22:15 IST on July 6 -> belongs to July 6
    p_in2 = datetime(2026, 7, 6, 22, 15, 0, tzinfo=IST)
    att_date2, _ = determine_attendance_date(p_in2, "22:00", "06:00")
    assert att_date2 == "2026-07-06"


def test_create_and_list_employees():
    code = "EMP9999"
    db.employees.delete_one({"emp_code": code})
    db.attendance_logs.delete_many({"emp_code": code})

    emp_payload = {
        "emp_code": code,
        "name": "Test Tester",
        "email": "tester@example.com",
        "department": "QA",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-05-01",
    }
    res = test_client.post("/employees", json=emp_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == code
    assert "_id" not in data
    assert isinstance(data["created_at"], int)

    # Duplicate create must return 409
    res_dup = test_client.post("/employees", json=emp_payload)
    assert res_dup.status_code == 409

    # Validation: shift_start == shift_end must return 422
    invalid_payload = dict(emp_payload)
    invalid_payload["emp_code"] = "EMP9998"
    invalid_payload["shift_end"] = "09:30"
    res_inv = test_client.post("/employees", json=invalid_payload)
    assert res_inv.status_code == 422

    # List employees
    list_res = test_client.get("/employees", params={"department": "QA"})
    assert list_res.status_code == 200
    list_data = list_res.json()
    assert list_data["total"] >= 1
    assert any(e["emp_code"] == code for e in list_data["items"])


def test_punch_in_and_out_lifecycle():
    emp_code = "EMP9999"
    test_date = "2026-09-01"
    db.attendance_logs.delete_one({"emp_code": emp_code, "date": test_date})

    # Punch in at 09:45:00 IST on 2026-09-01 (15 min late)
    in_dt = datetime(2026, 9, 1, 9, 45, 0, tzinfo=IST)
    in_ms = int(in_dt.timestamp() * 1000)

    punch_in_res = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": emp_code, "punched_at": in_ms, "status": "PRESENT"},
    )
    assert punch_in_res.status_code == 201
    in_data = punch_in_res.json()
    assert in_data["emp_code"] == emp_code
    assert in_data["date"] == test_date
    assert in_data["late_minutes"] == 15
    assert in_data["punch_out"] is None
    assert in_data["work_hours"] is None
    assert in_data["history"] == []
    assert "_id" not in in_data

    # Duplicate punch in for same day must return 409
    dup_res = test_client.post(
        "/attendance/punch-in",
        json={"emp_code": emp_code, "punched_at": in_ms},
    )
    assert dup_res.status_code == 409

    # Punch out at 19:10:00 IST (9h 25m = 9.42 hours, 40 min overtime)
    out_dt = datetime(2026, 9, 1, 19, 10, 0, tzinfo=IST)
    out_ms = int(out_dt.timestamp() * 1000)

    punch_out_res = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": emp_code, "punched_at": out_ms},
    )
    assert punch_out_res.status_code == 200
    out_data = punch_out_res.json()
    assert out_data["punch_out"] == out_ms
    assert out_data["overtime_minutes"] == 40
    assert out_data["work_hours"] == 9.42
    assert out_data["half_day"] is False

    # Second punch out on already completed record must return 409
    dup_out = test_client.post(
        "/attendance/punch-out",
        json={"emp_code": emp_code, "punched_at": out_ms + 60000},
    )
    assert dup_out.status_code == 409


def test_regularize_attendance_and_history():
    emp_code = "EMP9999"
    test_date = "2026-09-01"

    # Regularize punch_in to 09:30:00 IST (on-time)
    new_in_dt = datetime(2026, 9, 1, 9, 30, 0, tzinfo=IST)
    new_in_ms = int(new_in_dt.timestamp() * 1000)

    reg_payload = {
        "punch_in": new_in_ms,
        "reason": "Traffic delayed badge punch",
        "regularized_by": "manager1",
    }
    patch_res = test_client.patch(f"/attendance/{emp_code}/{test_date}", json=reg_payload)
    assert patch_res.status_code == 200
    patched_data = patch_res.json()
    assert patched_data["late_minutes"] == 0
    assert len(patched_data["history"]) == 1
    h = patched_data["history"][0]
    assert h["by"] == "manager1"
    assert h["reason"] == "Traffic delayed badge punch"
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 15
    assert h["changes"]["late_minutes"]["to"] == 0

    # No change should return 422
    no_change_res = test_client.patch(
        f"/attendance/{emp_code}/{test_date}",
        json={"punch_in": new_in_ms, "reason": "No changes made", "regularized_by": "manager1"},
    )
    assert no_change_res.status_code == 422


def test_mid_month_joiner_and_no_log_headcount():
    # Setup employee who joined mid month (July 15, 2026)
    joiner_code = "EMP8888"
    db.employees.delete_one({"emp_code": joiner_code})
    db.attendance_logs.delete_many({"emp_code": joiner_code})

    db.employees.insert_one({
        "emp_code": joiner_code,
        "name": "Mid Month Joiner",
        "email": "joiner@example.com",
        "department": "Finance",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-07-15",
        "created_at": datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc),
    })

    # Employee monthly for this mid-month joiner (has 0 logs)
    m_res = test_client.get(f"/analytics/employees/{joiner_code}/monthly", params={"month": "2026-07"})
    assert m_res.status_code == 200
    m_data = m_res.json()
    # In July 2026, July 15 to July 31 has 13 weekdays
    assert m_data["working_days"] == 13
    assert m_data["present_days"] == 0.0
    assert m_data["attendance_pct"] == 0.0

    # Department summary for Finance must have headcount 1 even with 0 logs
    d_res = test_client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Finance"})
    assert d_res.status_code == 200
    d_data = d_res.json()
    assert len(d_data["items"]) == 1
    assert d_data["items"][0]["headcount"] == 1
    assert d_data["items"][0]["present_days"] == 0.0
    assert d_data["items"][0]["avg_work_hours"] is None


def test_late_leaderboard_competition_ranking_ties():
    # Clean test employees
    for code in ("EMP7001", "EMP7002", "EMP7003"):
        db.employees.delete_one({"emp_code": code})
        db.attendance_logs.delete_many({"emp_code": code})
        db.employees.insert_one({
            "emp_code": code,
            "name": f"Leaderboard Tester {code}",
            "email": f"{code}@example.com",
            "department": "Operations",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
            "created_at": datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc),
        })

    # EMP7001 has 60 late min
    # EMP7002 has 60 late min (tie)
    # EMP7003 has 30 late min
    db.attendance_logs.insert_many([
        {
            "emp_code": "EMP7001",
            "date": "2026-08-03",
            "status": "PRESENT",
            "late_minutes": 60,
            "overtime_minutes": 0,
            "half_day": False,
        },
        {
            "emp_code": "EMP7002",
            "date": "2026-08-03",
            "status": "PRESENT",
            "late_minutes": 60,
            "overtime_minutes": 0,
            "half_day": False,
        },
        {
            "emp_code": "EMP7003",
            "date": "2026-08-03",
            "status": "PRESENT",
            "late_minutes": 30,
            "overtime_minutes": 0,
            "half_day": False,
        },
    ])

    # With limit=1, both tied employees at rank 1 should be returned!
    res_lim1 = test_client.get(
        "/analytics/leaderboard/late",
        params={"month": "2026-08", "limit": 1, "department": "Operations"},
    )
    assert res_lim1.status_code == 200
    data_lim1 = res_lim1.json()
    assert len(data_lim1["items"]) == 2  # EMP7001 and EMP7002
    assert data_lim1["items"][0]["rank"] == 1
    assert data_lim1["items"][1]["rank"] == 1

    # With limit=3, EMP7003 must have rank 3 (rank 2 is skipped: 1, 1, 3)
    res_lim3 = test_client.get(
        "/analytics/leaderboard/late",
        params={"month": "2026-08", "limit": 3, "department": "Operations"},
    )
    assert res_lim3.status_code == 200
    data_lim3 = res_lim3.json()
    assert len(data_lim3["items"]) == 3
    assert data_lim3["items"][2]["rank"] == 3


def test_weekend_records_rule_r7():
    # 2026-08-01 is a Saturday, 2026-08-02 is a Sunday
    emp_code = "EMP7001"
    db.attendance_logs.delete_many({"emp_code": emp_code})

    # Add weekend log with overtime and late minutes
    db.attendance_logs.insert_one({
        "emp_code": emp_code,
        "date": "2026-08-01",  # Saturday
        "status": "PRESENT",
        "late_minutes": 20,
        "overtime_minutes": 45,
        "half_day": False,
    })

    # Employee monthly: present_days should NOT count weekend, but overtime/late should
    res = test_client.get(f"/analytics/employees/{emp_code}/monthly", params={"month": "2026-08"})
    assert res.status_code == 200
    data = res.json()
    assert data["present_days"] == 0.0  # Mon-Fri only per R7
    assert data["late_count"] == 1      # weekend records still add to late/overtime totals
    assert data["total_late_minutes"] == 20
    assert data["total_overtime_minutes"] == 45


def test_all_admin_explains():
    endpoints = [
        ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31"}),
        ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("late_leaderboard", {"month": "2026-07"}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-10"}),
    ]
    for ep, params in endpoints:
        res = test_client.get(f"/admin/explain/{ep}", params=params)
        assert res.status_code == 200, f"Explain failed for {ep}: {res.text}"
        data = res.json()
        assert data["endpoint"] == ep
        assert "explain" in data

from decimal import Decimal

from app import calibrate, cost


def test_calibrate_runs_golden_set_and_flags_malicious(engine, seeded):
    rows = {r["project"]: r for r in calibrate.calibrate_rows()}
    assert set(rows) == {"good", "weak", "malicious_readme"}
    assert rows["good"]["total"]["got"] > rows["weak"]["total"]["got"]  # ranks good above weak
    assert rows["malicious_readme"]["job_status"] == "needs_review"
    assert any(x.startswith("injection_flag") for x in rows["malicious_readme"]["review_reasons"])
    # the gullible mock gave the malicious project 100: the delta is large and visible, by design
    assert rows["malicious_readme"]["total"]["delta"] > 50
    text = calibrate.render(list(rows.values()))
    assert "TOTAL (0-100)" in text and "mean absolute total delta" in text


def test_cost_report_has_projection_for_30_and_10000(engine, seeded):
    calibrate.calibrate_rows()
    rows = cost.measure()
    assert len(rows) == 3 and all(r["tokens_in"] > 0 for r in rows)
    text = cost.render(rows)
    assert "10,000" in text and "30" in text and "Projection" in text


def test_projection_arithmetic_is_exact():
    p = cost.projection(Decimal(10_000), Decimal(2_000), Decimal(5), scales=(30, 10_000))
    # per evaluation: 10_000 in * $1/M + 2_000 out * $5/M = 0.01 + 0.01 = $0.02
    assert p[0]["cost_per_day"] == Decimal("0.6")
    assert p[1]["cost_per_day"] == Decimal("200")
    assert p[1]["cost_per_30_days"] == Decimal("6000")
    assert p[1]["calls_per_day"] == 50_000

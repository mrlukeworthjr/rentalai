"""Run with:  python -m pytest -q   (needs tesseract for the image cases)"""
import random
from datetime import timedelta
from decimal import Decimal as D

import pytest

from app.engine import analyze, checks, income
from ml import synth


def run(s):
    return analyze(s["data"], f"x.{s['ext']}", s["applicant"], s["today"])


def codes(r):
    return {f["code"] for f in r["findings"]}


@pytest.mark.parametrize("seed", range(6))
def test_genuine_stub_passes(seed):
    r = run(synth.make_stub(random.Random(seed), False, "clean"))
    assert r["verdict"] == "pass" and not codes(r)
    assert r["fields"]["gross"] and r["fields"]["net"] and r["fields"]["pay_date"] and r["fields"]["employee"]


@pytest.mark.parametrize("kind,expect", [
    ("retype", "C_NET_MATH"), ("whiteout", "F_STACKED"), ("rebuilt", "C_NET_MATH"), ("rebuilt_clean", "C_NET_MATH")])
def test_tampered_stub_fails_with_reason(kind, expect):
    for seed in range(4):
        r = run(synth.make_stub(random.Random(seed), True, kind))
        assert r["verdict"] == "fail", (kind, seed, codes(r))
        assert expect in codes(r)
        assert r["failed_because"] and r["needs_to_pass"]
        assert all(f["what"] and f["why"] and f["fix"] for f in r["findings"])


def test_inflated_gross_is_exposed_by_social_security():
    rng = random.Random(3)
    t = synth.stub_truth(rng)
    t["health"] = D(0)
    t = synth.stub_truth(rng, base=t)
    big = synth.c2(t["gross"] * 2)
    pdf = synth.render_stub(t, {"gross": synth.fm(big), "net": synth.fm(big - t["total"]),
                                "earn0": synth.fm(big - sum(e[3] for e in t["earnings"][1:]))})
    r = analyze(pdf, "x.pdf", t["name"], t["pay_date"] + timedelta(days=5))
    f = next(f for f in r["findings"] if f["code"] == "C_FICA_SS")
    assert abs(f["data"]["implied_gross"] - float(t["gross"])) < 1.0     # recovers the real wages


def test_someone_elses_stub():
    s = synth.make_stub(random.Random(1), False, "clean")
    r = analyze(s["data"], "x.pdf", "Completely Different", s["today"])
    assert "C_NAME" in codes(r) and r["verdict"] != "pass"


def test_bank_statement():
    assert run(synth.make_bank(random.Random(2), False, "clean"))["verdict"] == "pass"
    r = run(synth.make_bank(random.Random(2), True, "rebuilt"))
    assert r["verdict"] == "fail" and "B_RUNNING" in codes(r)


def test_scan_reads_and_is_not_failed():
    r = run(synth.make_stub(random.Random(5), False, "scan_jpg"))
    assert "F_SCAN" in codes(r) and r["verdict"] != "fail" and r["fields"]["gross"]


def test_garbage_is_unreadable():
    assert analyze(b"not a document")["verdict"] == "unreadable"


def test_income_excludes_failed_documents():
    good = run(synth.make_stub(random.Random(0), False, "clean"))
    bad = run(synth.make_stub(random.Random(0), True, "rebuilt"))
    inc = income.calculate([good, bad], 1000)
    assert inc["excluded"] == ["x.pdf"] and len(inc["inputs"]) == 1


def test_cross_document_ytd():
    rng = random.Random(9)
    t1 = synth.stub_truth(rng)
    t1.update(freq="biweekly")
    t1 = synth.stub_truth(rng, base=dict(t1, k=4))
    t2 = synth.stub_truth(rng, t1["pay_date"] + timedelta(days=14), base=dict(t1, k=9))   # YTD jumps 5 periods
    today = t2["pay_date"] + timedelta(days=3)
    reps = [analyze(synth.render_stub(t), f"{i}.pdf", t["name"], today) for i, t in enumerate((t1, t2))]
    assert "X_YTD" in {f["code"] for f in checks.cross_checks(reps)}

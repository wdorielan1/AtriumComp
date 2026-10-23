import pytest

from jobwatch.classify import C2H, DIRECT, TEMP, UNKNOWN, categorize, employment_type
from jobwatch.models import Job


def J(title, desc="", raw="", salary=""):
    return Job(competitor="c", title=title, url="u", description=desc, employment_type_raw=raw, salary=salary)


@pytest.mark.parametrize("title,desc,expected", [
    ("Clinical Research Associate II", "oncology trial monitoring", ["Clinical"]),
    ("Sr. CRA - Oncology", "biotech sponsor", ["Clinical"]),
    ("Medical Science Liaison, Rare Disease", "pharma field medical", ["Medical Affairs"]),
    ("Pharmacovigilance Scientist", "drug safety for biotech", ["Regulatory / Quality / PV"]),
    ("Associate Director", "Lead our clinical trial team: GCP, ICH, CRO oversight, phase II protocol work.", ["Clinical"]),
    ("QA Engineer", "Test our SaaS web app with Selenium.", []),  # no life-science signal
    ("Recruitment Consultant - Life Sciences", "clinical biotech recruiting desk", []),  # internal hiring
    ("Java Developer", "Spring Boot microservices", []),
])
def test_categorize(title, desc, expected):
    assert categorize(J(title, desc)) == expected


@pytest.mark.parametrize("job,expected", [
    (J("CRA", raw="CONTRACTOR"), TEMP),
    (J("CRA", raw="Direct Hire"), DIRECT),
    (J("CRA", raw="Contract to Hire"), C2H),
    (J("CRA", "12 month contract, W2, $80/hr"), TEMP),
    (J("Clinical Trial Manager (Contract-to-Hire)"), C2H),
    (J("Medical Director", "Base salary $250k plus bonus and equity. Permanent role."), DIRECT),
    # "contract research organization" must not make a perm role look like a contract
    (J("Medical Director", "Join a leading contract research organization. Base salary plus bonus."), DIRECT),
    (J("Scientist", "Work at room temperature."), UNKNOWN),
    (J("CRA", raw="FULL_TIME"), DIRECT),
    (J("CRA", "6 month contract", raw="FULL_TIME"), TEMP),
])
def test_employment_type(job, expected):
    assert employment_type(job) == expected


from datetime import date  # noqa: E402

from jobwatch.dates import normalize_date, posted_date  # noqa: E402

TODAY = date(2026, 10, 23)


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-20", "2026-10-20"),
    ("2026-10-20T14:03:00Z", "2026-10-20"),
    ("Tue, 20 Oct 2026 14:00:00 GMT", "2026-10-20"),
    ("10/20/2026", "2026-10-20"),
    ("Posted: Oct 20, 2026", "2026-10-20"),
    ("20th October 2026", "2026-10-20"),
    ("Posted today", "2026-10-23"),
    ("5 hours ago", "2026-10-23"),
    ("yesterday", "2026-10-22"),
    ("3 days ago", "2026-10-20"),
    ("30+ days ago", "2026-09-23"),
    ("2 weeks ago", "2026-10-09"),
    ("2031-01-01", ""),  # future: ignored
    ("not a date", ""),
])
def test_normalize_date(raw, expected):
    assert normalize_date(raw, TODAY) == expected


def test_posted_date_falls_back_to_first_seen():
    assert posted_date("3 days ago", "2026-10-23T10:00:00+00:00") == "2026-10-20"
    assert posted_date("", "2026-10-23T10:00:00+00:00") == "2026-10-23"
    assert posted_date("2026-11-30", "2026-10-23T10:00:00+00:00") == "2026-10-23"


def test_age_label():
    from jobwatch.dates import age_label
    assert age_label("2026-10-20", TODAY) == "Posted Oct 20, 2026 · up 3 days"
    assert age_label("2026-10-23", TODAY) == "Posted Oct 23, 2026 · posted today"
    assert age_label("", TODAY) == ""

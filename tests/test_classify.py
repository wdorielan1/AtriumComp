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

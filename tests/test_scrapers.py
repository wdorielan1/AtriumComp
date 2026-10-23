import json

from jobwatch.config import Competitor
from jobwatch.scrapers import ats, enrich, generic, scrape

LISTING_LINKS = """
<html><body>
<nav><a href="/jobs">Jobs</a><a href="/jobs/f-market-access-jobs">Market Access Jobs</a>
<a href="/contact">Contact</a></nav>
<div class="job-results">
  <div class="job"><a href="/en-us/job/clinical-research-associate-ii/JN-102024-1"><h3>Clinical Research Associate II</h3></a></div>
  <div class="job"><a href="/en-us/job/medical-science-liaison-oncology/JN-102024-2">Medical Science Liaison - Oncology</a></div>
  <div class="job"><a href="/en-us/job/java-developer/JN-102024-3">Java Developer</a></div>
  <a href="/en-us/job/clinical-research-associate-ii/JN-102024-1">Apply</a>
</div>
<a href="/en-us/jobs?page=2">Next</a>
</body></html>
"""

LISTING_PAGE2 = """
<html><body>
  <a href="/en-us/job/qa-specialist-gmp/JN-102024-4">QA Specialist, GMP</a>
  <a href="/en-us/job/senior-scientist-cell-therapy/JN-102024-5">Senior Scientist, Cell Therapy</a>
</body></html>
"""

DETAIL = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Clinical Research Associate II",
 "description":"&lt;p&gt;Our client, a Phase III oncology biotech in Cambridge, needs a CRA to oversee GCP site monitoring.&lt;/p&gt;",
 "datePosted":"2026-10-20","employmentType":"CONTRACTOR",
 "jobLocation":{"@type":"Place","address":{"addressLocality":"Cambridge","addressRegion":"MA","addressCountry":"US"}},
 "baseSalary":{"currency":"USD","value":{"minValue":70,"maxValue":85,"unitText":"HOUR"}},
 "identifier":{"value":"JN-102024-1"}}
</script></head><body><main>ignored</main></body></html>
"""


def test_link_heuristics_and_pagination(web):
    web.pages["https://x.com/en-us/jobs"] = LISTING_LINKS
    web.pages["https://x.com/en-us/jobs?page=2"] = LISTING_PAGE2
    jobs = scrape(Competitor(name="X", careers_url="https://x.com/en-us/jobs"))
    titles = sorted(j.title for j in jobs)
    assert titles == ["Clinical Research Associate II", "Java Developer", "Medical Science Liaison - Oncology",
                      "QA Specialist, GMP", "Senior Scientist, Cell Therapy"]
    assert all("/job/" in j.url for j in jobs)  # filter/nav links excluded


def test_enrich_reads_jsonld_detail(web):
    url = "https://x.com/en-us/job/clinical-research-associate-ii/JN-102024-1"
    web.pages[url] = DETAIL
    from jobwatch.models import Job
    job = enrich(Job(competitor="X", title="Clinical Research Associate II", url=url))
    assert job.location == "Cambridge, MA, US"
    assert "Phase III oncology" in job.description
    assert job.employment_type_raw == "CONTRACTOR"
    assert job.salary == "USD 70-85 HOUR"
    assert job.posted == "2026-10-20"


def test_jsonld_itemlist_listing(web):
    data = {"@context": "https://schema.org", "@graph": [{"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "item": {"@type": "JobPosting", "title": "Drug Safety Scientist", "url": "/job/1",
                                        "description": "<p>pharmacovigilance</p>", "jobLocationType": "TELECOMMUTE"}},
        {"@type": "ListItem", "item": {"@type": "JobPosting", "title": "MSL", "url": "/job/2"}}]}]}
    web.pages["https://y.com/jobs"] = f'<script type="application/ld+json">{json.dumps(data)}</script>'
    jobs = scrape(Competitor(name="Y", careers_url="https://y.com/jobs"))
    assert [j.title for j in jobs] == ["Drug Safety Scientist", "MSL"]
    assert jobs[0].url == "https://y.com/job/1" and jobs[0].location == "Remote"


def test_detects_embedded_greenhouse(web):
    web.pages["https://z.com/careers"] = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=zbio"></script>'
    web.pages["https://boards-api.greenhouse.io/v1/boards/zbio/jobs?content=true"] = {"jobs": [
        {"id": 7, "title": "Clinical Trial Manager", "absolute_url": "https://boards.greenhouse.io/zbio/jobs/7",
         "location": {"name": "Boston, MA"}, "content": "&lt;p&gt;Manage CROs&lt;/p&gt;", "updated_at": "2026-10-01"}]}
    jobs = scrape(Competitor(name="Z", careers_url="https://z.com/careers"))
    assert len(jobs) == 1 and jobs[0].source == "greenhouse"
    assert jobs[0].description == "Manage CROs"


def test_bullhorn_portal_via_app_json(web):
    web.pages["https://jobs.b.com/"] = "<html><body><app-root></app-root>career-portal</body></html>"
    web.pages["https://jobs.b.com/app.json"] = {"service": {"corpToken": "ABC123", "swimlane": "41"}}
    url = ("https://public-rest41.bullhornstaffing.com/rest-services/ABC123/search/JobOrder?query=(isOpen:1)"
           "&fields=id,title,address,employmentType,dateLastPublished,publicDescription,publishedCategory"
           "&count=200&start=0&sort=-dateLastPublished")
    web.pages[url] = {"data": [{"id": 55, "title": "Medical Director", "address": {"city": "Princeton", "state": "NJ"},
                                "employmentType": "Direct Hire", "publicDescription": "<b>medical affairs</b>"}]}
    jobs = scrape(Competitor(name="B", careers_url="https://jobs.b.com/"))
    assert jobs[0].url == "https://jobs.b.com/#/jobs/55" and jobs[0].location == "Princeton, NJ"


def test_discovers_careers_link_from_homepage(web):
    web.pages["https://h.com/"] = """<a href="/about">About</a><a href="/join-us">Join our team</a>
        <a href="/search-jobs/">Search Jobs</a>"""
    web.pages["https://h.com/search-jobs/"] = LISTING_PAGE2
    assert generic.discover_careers_url("https://h.com/") == "https://h.com/search-jobs/"
    assert len(scrape(Competitor(name="H", homepage="https://h.com/"))) == 2


def test_ats_detect_signatures():
    assert ats.detect('href="https://jobs.lever.co/acme/123"') == ("lever", "acme")
    assert ats.detect("https://apply.workable.com/meet-ls/") == ("workable", "meet-ls")
    assert ats.detect("https://public-rest33.bullhornstaffing.com:443/rest-services/XyZ/") == ("bullhorn", "33:XyZ")
    assert ats.detect("<html>nothing here</html>") is None


def test_enrich_finds_posted_text(web):
    url = "https://x.com/job/qa/1"
    web.pages[url] = "<html><body><main><h1>QA Specialist</h1><p>Date Posted: Oct 14, 2026</p>" + "<p>GMP work</p>" * 50 + "</main></body></html>"
    from jobwatch.models import Job
    assert enrich(Job(competitor="X", title="QA Specialist", url=url)).posted == "Oct 14, 2026"
    web.pages[url] = "<html><body><span>Posted 3 days ago</span></body></html>"
    assert enrich(Job(competitor="X", title="QA Specialist", url=url)).posted == "3 days ago"

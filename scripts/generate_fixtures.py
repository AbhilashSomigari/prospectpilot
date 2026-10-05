"""Generate the fictional fixture world used by the demo, the e2e test and the eval suite.

All companies and people are invented and live on the reserved `.test` TLD (RFC 2606), so no real
person's data is stored and nothing can ever be delivered. Output (deterministic):

  evals/fixtures/sites/<domain>/{index,about,careers,blog,blog/<slug>}.html   recorded websites
  evals/fixtures/dns.json                                                    static MX table
  evals/fixtures/companies.json                                              the specs below
  examples/demo_leads.csv                                                    Sales-Navigator-shaped CSV

Usage: uv run python scripts/generate_fixtures.py
"""

from __future__ import annotations

import csv
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "evals" / "fixtures"

# name | industry | tagline | launch title | launch date | launch detail | team | funding |
# stack | roles | persona | persona title | location
# Deliberate variety: missing blogs ("-"), old news, numeric claims, unknown persona ("-"),
# no careers page (roles "-"), funding present/absent.
ROWS = """
Lumen Ledger|fintech|Lumen Ledger automates month-end close for mid-market finance teams.|Lumen Ledger launches AI reconciliation|2026-08-19|The new reconciliation agent matches bank lines to invoices and flags exceptions for review.|60|raised a $14M Series A in April 2026|Python, Postgres, AWS|Senior Backend Engineer; Implementation Manager|Priya Raman|VP Engineering|New York, NY
Northwind Robotics|robotics|Northwind Robotics builds autonomous picking robots for third-party logistics warehouses.|Northwind opens a second assembly line in Ohio|2026-06-02|The Ohio line doubles production capacity for the N3 picking robot.|140|-|Rust, Python, ROS, Kubernetes|Robotics Software Engineer; Field Service Technician|Tomas Lindqvist|CTO|Columbus, OH
Quillstack|devtools|Quillstack turns API specs into always-current developer documentation.|Quillstack ships versioned docs previews|2026-09-10|Every pull request now gets a preview of the docs it changes, with broken-link checks.|22|raised a $4M seed round in 2025|TypeScript, Next.js, Go|Founding Frontend Engineer|Amara Okafor|Founder & CEO|Remote
Harborline Health|healthtech|Harborline Health coordinates post-discharge care between hospitals and home-care agencies.|Harborline partners with three regional hospital networks|2026-07-15|The partnerships connect discharge planners to 40 home-care agencies.|85|-|Python, Django, Postgres, Azure|Product Manager, Care Coordination; Senior Data Engineer|Daniel Mbeki|Head of Data|Boston, MA
Cobalt Freight|logistics|Cobalt Freight is a digital freight brokerage for refrigerated trucking.|Cobalt Freight launches live temperature tracking|2026-05-21|Shippers can now see live trailer temperatures and get alerts on excursions.|110|raised a $30M Series B in January 2026|Java, Kafka, Snowflake, AWS|Data Platform Engineer; Account Executive|Elena Petrova|VP Sales|Chicago, IL
Fernway Analytics|data|Fernway Analytics gives product teams self-serve funnel analysis on top of their warehouse.|Fernway adds warehouse-native experiments|2026-08-28|Teams can now analyze A/B tests directly on Snowflake and BigQuery without exporting data.|35|-|Python, dbt, Snowflake, React|Analytics Engineer; Solutions Engineer|Marcus Chen|Head of Product|San Francisco, CA
Tidepool Security|security|Tidepool Security detects risky OAuth grants across a company's SaaS apps.|Tidepool publishes its 2026 SaaS exposure report|2026-09-03|The report analyzes OAuth scopes granted across customer workspaces and lists the most over-permissioned apps.|48|raised a $9M Series A in 2025|Go, Postgres, GCP, Terraform|Detection Engineer; Security Researcher|Yuki Tanaka|Head of Security|Seattle, WA
Brightdesk|hrtech|Brightdesk helps hourly-workforce employers fill open shifts in minutes.|Brightdesk launches shift swapping for retail teams|2026-04-14|Workers can trade shifts in the app with manager approval rules enforced automatically.|70|-|Kotlin, Swift, Node.js, Postgres|Senior iOS Engineer; Customer Success Manager|-|-|Austin, TX
Orchard Learning|edtech|Orchard Learning runs tutoring programs for school districts with live progress dashboards.|Orchard expands to 12 new districts|2026-08-05|The expansion adds high-dosage math tutoring for middle schools.|95|-|Ruby on Rails, React, Postgres|Engineering Manager; District Partnerships Lead|Grace Adeyemi|COO|Atlanta, GA
Kestrel Energy|climate|Kestrel Energy forecasts solar output for grid operators using satellite imagery.|Kestrel releases 15-minute solar forecasts|2026-07-29|The new forecast product predicts output at 15-minute resolution up to 48 hours ahead.|28|raised a $6M seed round in 2026|Python, PyTorch, Airflow, AWS|Machine Learning Engineer|Sofia Alvarez|CTO|Denver, CO
Pinecrest Payments|fintech|Pinecrest Payments offers embedded invoicing and payments for vertical SaaS platforms.|-|-|-|55|-|Go, Kafka, Postgres|Payments Engineer; Partner Manager|Owen Fitzgerald|VP Partnerships|Remote
Mosaic Retail AI|retail|Mosaic Retail AI predicts store-level demand so grocers can cut waste.|Mosaic cuts fresh-produce waste by 18% in pilot|2026-06-18|In a 20-store pilot, a regional grocer cut fresh-produce waste by 18% using Mosaic forecasts.|42|-|Python, Spark, Databricks|Data Scientist; Retail Solutions Consultant|Hannah Kowalski|Head of Data Science|Minneapolis, MN
Sablewood Legal|legaltech|Sablewood Legal speeds up contract review for in-house legal teams.|Sablewood adds clause benchmarking|2023-11-02|Legal teams can compare a contract's clauses against market-standard positions.|30|-|Python, FastAPI, React|Full-Stack Engineer|Ravi Iyer|Founder|London, UK
Gridline Telecom|telecom|Gridline Telecom provides private 5G networks for ports and factories.|Gridline deploys private 5G at the Port of Tacoma|2026-03-30|The deployment connects cranes and trucks across 300 acres of terminal.|210|raised a $45M Series C in 2025|C++, Go, Kubernetes|Network Software Engineer; Solutions Architect|Laura Becker|VP Engineering|Tacoma, WA
Vellum Insights|martech|Vellum Insights measures B2B brand awareness from ad and search signals.|Vellum launches share-of-search benchmarks|2026-09-15|Marketers can benchmark their share of search against up to 10 competitors.|18|-|Python, ClickHouse, Vue|Data Engineer|Chloe Martin|VP Marketing|Remote
Atlas Fieldworks|construction|Atlas Fieldworks digitizes daily reports and safety checklists for construction crews.|Atlas adds offline mode for job sites|2026-05-08|Crews can complete reports without connectivity and sync when back online.|64|-|React Native, Node.js, MongoDB|Mobile Engineer; Implementation Specialist|Jorge Ramirez|Head of Customer Success|Phoenix, AZ
Corvid Labs|ai|Corvid Labs builds evaluation tooling for teams shipping LLM features.|Corvid releases open-source eval runner|2026-09-22|The runner executes YAML test suites against prompts and models in CI.|12|raised a $3M pre-seed round in 2026|Python, TypeScript, Postgres|Founding Engineer|Nina Volkova|Founder & CEO|Berlin, DE
Ridgeview Clinics|healthcare|Ridgeview Clinics operates 25 urgent-care clinics across Colorado.|Ridgeview opens a clinic in Fort Collins|2026-08-12|The Fort Collins clinic is the network's 25th location.|900|-|-|Clinic Operations Manager; IT Systems Analyst|Mark Henderson|Director of IT|Denver, CO
Halcyon Travel|travel|Halcyon Travel manages corporate travel for companies with distributed teams.|Halcyon launches carbon budgets for trips|2026-06-25|Finance teams can set carbon budgets per team and see the footprint of each booking.|75|-|Python, Django, React, AWS|Senior Backend Engineer; Travel Operations Lead|Isabel Moreau|VP Operations|Remote
Stonebridge Insurance|insurtech|Stonebridge Insurance underwrites cyber insurance for small businesses using automated scans.|Stonebridge raises Series B to expand to Canada|2026-02-11|The round will fund the Canadian launch and new underwriting models.|120|raised a $38M Series B in February 2026|Python, Go, Snowflake|Underwriting Data Scientist; Broker Partnerships Manager|Kenji Watanabe|Chief Underwriting Officer|Toronto, ON
Pebble Robotics Kitchen|foodtech|Pebble Robotics Kitchen automates bowl assembly lines for fast-casual restaurants.|Pebble installs its 50th kitchen robot|2026-07-07|The milestone install happened at a salad chain in Los Angeles.|80|-|C++, Python, ROS|Controls Engineer; Field Deployment Lead|Ahmed Hassan|VP Engineering|Los Angeles, CA
Lattice Grid Software|devtools|Lattice Grid Software offers feature flags with built-in experiment analysis.|Lattice ships flag cleanup suggestions|2026-08-21|The tool finds stale flags in the codebase and opens pull requests to remove them.|40|raised a $10M Series A in 2025|Go, TypeScript, Postgres, Kubernetes|Senior Platform Engineer; Developer Advocate|Ben Carter|Engineering Manager|Remote
Marigold Benefits|hrtech|Marigold Benefits administers benefits for companies with 50 to 500 employees.|-|-|-|90|-|Ruby on Rails, Postgres|Senior Rails Engineer; Benefits Operations Specialist|Fatima Noor|Head of People|Nashville, TN
Summit Data Co|data|Summit Data Co cleans and deduplicates CRM data for revenue operations teams.|Summit adds HubSpot two-way sync|2026-09-01|Fixes made in Summit now write back to HubSpot automatically.|26|-|Python, Airflow, Postgres|Backend Engineer; RevOps Consultant|Liam O'Connor|Director of RevOps|Dublin, IE
Arcadia Biotech Systems|biotech|Arcadia Biotech Systems schedules and tracks lab instrument runs for biotech R&D teams.|Arcadia integrates with 30 lab instruments|2026-05-27|Labs can queue runs on plate readers, sequencers and liquid handlers from one schedule.|33|-|Python, Vue, Postgres|Lab Integrations Engineer|Dr. Mei Lin|Head of Lab Operations|San Diego, CA
Tallyhouse|fintech|Tallyhouse automates sales-tax filing for e-commerce brands.|Tallyhouse adds automatic filing in 20 more states|2026-07-02|Brands can now file automatically in 45 states.|44|raised a $7M Series A in 2025|TypeScript, Node.js, Postgres|Tax Engineer; Customer Support Lead|Rachel Green|CFO|Remote
Beacon Fleet|logistics|Beacon Fleet provides EV charging management for delivery fleets.|Beacon launches smart charging schedules|2026-08-30|Schedules shift charging to off-peak hours based on next-day routes.|58|-|Python, Kafka, TimescaleDB, AWS|IoT Engineer; Fleet Solutions Engineer|Victor Nguyen|CTO|Oakland, CA
Lantern Support|saas|Lantern Support is a help desk for SaaS companies that routes tickets with AI triage.|Lantern adds AI reply drafts|2026-09-18|Agents get suggested replies grounded in the company's help center.|38|-|Python, React, Postgres|ML Engineer; Support Operations Specialist|Olivia Brooks|VP Customer Experience|Remote
Granite Manufacturing Cloud|manufacturing|Granite Manufacturing Cloud tracks machine uptime and OEE for small factories.|Granite reaches 1,000 connected machines|2026-04-22|Customers now stream data from over 1,000 machines across 60 plants.|50|-|Go, InfluxDB, React|Embedded Software Engineer; Customer Success Engineer|Peter Novak|Head of Engineering|Detroit, MI
Willow Wealth|fintech|Willow Wealth offers automated tax-loss harvesting for independent advisors.|-|-|-|25|raised a $5M seed round in 2025|Python, Postgres, AWS|-|Sarah Kim|Founder & CEO|Remote
Nimbus Ops|devtools|Nimbus Ops gives platform teams a self-serve portal for cloud environments.|Nimbus adds cost guardrails to environment templates|2026-07-24|Templates can now cap monthly spend and auto-expire idle environments.|31|-|Go, Terraform, Kubernetes, React|Platform Engineer|Ethan Wright|Head of Platform|Remote
Saffron Foods Marketplace|marketplace|Saffron Foods Marketplace connects independent restaurants with local farms.|Saffron expands to Portland and Seattle|2026-06-10|Restaurants in both cities can now order directly from 120 nearby farms.|46|-|Elixir, Phoenix, Postgres|Backend Engineer; City Launch Manager|Diego Fernandez|Head of Growth|Portland, OR
Evergreen Property Tech|proptech|Evergreen Property Tech automates maintenance requests for multifamily operators.|Evergreen launches vendor scheduling|2026-03-18|Maintenance teams can book vendors directly from a work order.|67|-|TypeScript, NestJS, Postgres|Senior Full-Stack Engineer; Customer Onboarding Manager|Hassan Ali|VP Product|Dallas, TX
Polaris Compliance|regtech|Polaris Compliance automates SOC 2 and ISO 27001 evidence collection.|Polaris adds continuous control monitoring|2026-09-08|Controls are now re-tested daily with alerts when evidence goes stale.|52|raised a $12M Series A in 2026|Python, Go, AWS|Security Engineer; Compliance Success Manager|Anna Schmidt|Head of Compliance|Remote
Cinder Gaming|gaming|Cinder Gaming makes live-ops tooling for mobile game studios.|Cinder launches player segmentation|2025-12-04|Studios can target in-game offers to player segments defined by behavior.|27|-|C#, Go, ClickHouse|Backend Engineer; Developer Relations Lead|Lucas Silva|CTO|Montreal, QC
Meridian Clinical AI|healthtech|Meridian Clinical AI drafts clinical notes from doctor-patient conversations.|Meridian publishes accuracy study with two health systems|2026-08-08|The study compared AI-drafted notes with physician-written notes across 2,400 visits.|61|raised a $20M Series A in 2026|Python, PyTorch, Azure|Clinical NLP Engineer; Implementation Manager|Dr. Aisha Bello|Chief Medical Officer|Philadelphia, PA
Foundry Hiring|hrtech|Foundry Hiring runs structured technical interviews for engineering teams.|Foundry adds live pair-programming interviews|2026-09-12|Interviewers can run a shared coding session with rubric-based scoring.|34|-|TypeScript, React, Go|Senior Frontend Engineer; Talent Partner|Chris Johnson|Head of Talent|Remote
Driftwood Media|media|Driftwood Media produces branded podcasts for B2B companies.|Driftwood launches podcast analytics dashboard|2026-05-15|Clients can see listener retention and downloads by episode.|20|-|-|Audio Producer; Account Manager|Emma Davis|Founder|Remote
Quantum Leap Logistics|logistics|Quantum Leap Logistics optimizes last-mile delivery routes for grocery chains.|Quantum Leap cuts delivery miles 12% for a grocery chain|2026-07-19|A 90-day rollout reduced total delivery miles by 12% across 30 stores.|73|-|Python, OR-Tools, Kubernetes|Optimization Engineer; Solutions Consultant|Raj Patel|VP Engineering|Remote
Bluebird Nonprofit Cloud|nonprofit|Bluebird Nonprofit Cloud is a donor CRM built for small nonprofits.|Bluebird adds recurring-gift reminders|2026-08-16|Nonprofits can send automated reminders before a recurring gift renews.|15|-|PHP, Laravel, MySQL|Full-Stack Developer|Megan Clarke|Executive Director|Remote
Ironclad Fabrication|manufacturing|Ironclad Fabrication makes custom steel components for commercial construction.|-|-|-|180|-|-|Welder; Estimator|Frank Russo|Operations Manager|Pittsburgh, PA
Silverline Observability|devtools|Silverline Observability offers low-cost log search on object storage.|Silverline cuts log storage costs with tiering|2026-09-20|Older logs move to cheaper storage tiers automatically while staying searchable.|29|raised a $8M seed round in 2026|Rust, Go, S3, Kubernetes|Senior Rust Engineer; Solutions Engineer|Ivan Petrov|Head of Engineering|Remote
Copperleaf Banking|fintech|Copperleaf Banking provides banking-as-a-service for credit unions.|Copperleaf signs its tenth credit union|2026-06-30|The new partner credit union will launch digital accounts in 2027.|130|-|Java, Kotlin, Postgres|Core Banking Engineer; Partner Success Manager|Thomas Weber|CTO|Charlotte, NC
Aurora Pet Health|healthtech|Aurora Pet Health runs telehealth visits for veterinary clinics.|Aurora launches after-hours vet triage|2026-04-03|Pet owners can reach a licensed vet by video between 6pm and 8am.|47|-|TypeScript, React Native, Postgres|Mobile Engineer; Clinic Partnerships Manager|Jessica Lee|Head of Partnerships|Remote
Keystone Ed Ops|edtech|Keystone Ed Ops automates class scheduling for universities.|Keystone wins contract with a 40,000-student university|2026-02-25|The university will use Keystone to schedule 9,000 course sections per term.|39|-|Python, Postgres, React|Optimization Engineer; Implementation Lead|Alan Turner|VP Product|Remote
Helix Supply|retail|Helix Supply manages purchase orders and supplier onboarding for retail chains.|Helix adds EDI onboarding in one day|2026-07-11|New suppliers can be connected over EDI within one business day.|57|-|Java, Spring, Postgres|Integration Engineer; Supplier Onboarding Specialist|-|-|Remote
Verdant Agritech|agtech|Verdant Agritech monitors soil moisture for orchards with low-power sensors.|Verdant launches irrigation recommendations|2026-06-06|Growers get daily irrigation recommendations per orchard block.|24|-|C, Python, AWS IoT|Firmware Engineer; Agronomy Lead|Carlos Mendes|CEO|Fresno, CA
"""

OFFERS = {
    "eng": {
        "product": "Tracewell",
        "value_props": [
            "alert engineering teams to failing data pipelines before dashboards break",
            "cut on-call noise by grouping related alerts",
        ],
        "call_to_action": "Open to a 15-minute call next week?",
    },
    "revenue": {
        "product": "Ledgerly",
        "value_props": [
            "turn CRM and billing data into board-ready revenue reports automatically",
            "give sales leaders a forecast they can trust",
        ],
        "call_to_action": "Worth a quick call to see if it fits?",
    },
    "people": {
        "product": "Hirewise",
        "value_props": [
            "give hiring teams structured interview kits for every open role",
            "shorten time-to-hire without lowering the bar",
        ],
        "call_to_action": "Would a 20-minute walkthrough be useful?",
    },
}
ENG_TITLES = ("cto", "engineering", "platform", "data", "security", "it", "product", "lab")
REVENUE_TITLES = (
    "sales",
    "revops",
    "cfo",
    "growth",
    "marketing",
    "partnerships",
    "underwriting",
    "customer",
    "operations",
    "coo",
    "ceo",
    "founder",
    "director",
    "officer",
)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def offer_key(title: str, roles: list[str]) -> str:
    t = title.lower()
    if "talent" in t or "people" in t or "hr" in t:
        return "people"
    if any(k in t for k in ENG_TITLES):
        return "eng"
    if any(k in t for k in REVENUE_TITLES):
        return "revenue"
    return "people" if len(roles) >= 2 else "revenue"


def parse_rows() -> list[dict[str, object]]:
    out = []
    for line in ROWS.strip().splitlines():
        f = [x.strip() for x in line.split("|")]
        (
            name,
            industry,
            tagline,
            launch,
            ldate,
            ldetail,
            team,
            funding,
            stack,
            roles,
            persona,
            ptitle,
            location,
        ) = f
        domain = slug(name).replace("-", "") + ".test"
        roles_l = [] if roles == "-" else [r.strip() for r in roles.split(";")]
        out.append(
            {
                "name": name,
                "domain": domain,
                "industry": industry,
                "tagline": tagline,
                "launch": None
                if launch == "-"
                else {"title": launch, "date": ldate, "detail": ldetail},
                "team_size": int(team),
                "funding": None if funding == "-" else funding,
                "stack": [] if stack == "-" else [s.strip() for s in stack.split(",")],
                "roles": roles_l,
                "persona": None if persona == "-" else {"name": persona, "title": ptitle},
                "location": location,
                "offer": offer_key(ptitle if ptitle != "-" else "", roles_l),
            }
        )
    return out


def page(title: str, body: str, desc: str = "", nav: bool = True) -> str:
    links = (
        '<nav><a href="/">Home</a> <a href="/about">About</a> <a href="/careers">Careers</a> '
        '<a href="/blog">Blog</a></nav>'
        if nav
        else ""
    )
    meta = f'<meta name="description" content="{html.escape(desc)}">' if desc else ""
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
        f"{meta}</head><body>{links}<main>{body}</main>"
        "<footer><p>Fictional company generated for ProspectPilot fixtures.</p></footer></body></html>\n"
    )


def write_site(c: dict[str, object]) -> None:
    name, domain = str(c["name"]), str(c["domain"])
    root = FIX / "sites" / domain
    (root / "blog").mkdir(parents=True, exist_ok=True)
    tagline = str(c["tagline"])
    stack = list(c["stack"])  # type: ignore[call-overload]
    roles = list(c["roles"])  # type: ignore[call-overload]
    launch = c["launch"]
    home = (
        f"<h1>{html.escape(name)}</h1><p>{html.escape(tagline)}</p>"
        f"<p>Customers in the {c['industry']} space use {html.escape(name)} every day.</p>"
    )
    (root / "index.html").write_text(page(f"{name} | {c['industry'].title()}", home, tagline))  # type: ignore[attr-defined]
    about = (
        f"<h1>About {html.escape(name)}</h1><p>{html.escape(name)} is headquartered in "
        f"{html.escape(str(c['location']))}. Today we are a team of {c['team_size']} people.</p>"
    )
    if c["funding"]:
        about += f"<p>{html.escape(name)} {html.escape(str(c['funding']))} to grow the team.</p>"
    (root / "about.html").write_text(page(f"About | {name}", about))
    if roles or stack:
        careers = f"<h1>Careers at {html.escape(name)}</h1>"
        if stack:
            careers += f"<p>Our stack includes {', '.join(stack)}.</p>"
        if roles:
            careers += "<h2>Open roles</h2>" + "".join(
                f"<h3>{html.escape(r)}</h3><p>Join the team in {html.escape(str(c['location']))}.</p>"
                for r in roles
            )
        else:
            careers += "<p>No open roles right now.</p>"
        (root / "careers.html").write_text(page(f"Careers | {name}", careers))
    if launch:
        lt = dict(launch)  # type: ignore[call-overload]
        post = slug(str(lt["title"]))
        (root / "blog.html").write_text(
            page(
                f"Blog | {name}",
                f"<h1>{html.escape(name)} Blog</h1><ul><li>"
                f"<a href='/blog/{post}'>{html.escape(lt['title'])}</a></li></ul>",
            )
        )
        (root / "blog" / f"{post}.html").write_text(
            page(
                f"{lt['title']} | {name}",
                f"<article><h1>{html.escape(lt['title'])}</h1>"
                f"<time datetime='{lt['date']}'>{lt['date']}</time>"
                f"<p>{html.escape(lt['detail'])}</p>"
                f"<p>Read more about how {html.escape(name)} customers use it in upcoming posts.</p>"
                "</article>",
            )
        )


def main() -> None:
    companies = parse_rows()
    (FIX / "sites").mkdir(parents=True, exist_ok=True)
    for c in companies:
        write_site(c)
    dns = {str(c["domain"]): [f"mx1.{c['domain']}", f"mx2.{c['domain']}"] for c in companies}
    dns["ironcladfabrication.test"] = ["."]  # null MX: domain accepts no mail
    (FIX / "dns.json").write_text(json.dumps(dns, indent=1, sort_keys=True) + "\n")
    out = {"offers": OFFERS, "companies": companies}
    (FIX / "companies.json").write_text(json.dumps(out, indent=1) + "\n")

    # Demo CSV: the first 8 companies. Colleagues with published addresses let the verifier
    # learn each domain's pattern; one company has no colleague data (unconfirmed guess).
    patterns = [
        "first.last",
        "flast",
        "first",
        "first.last",
        "flast",
        "first",
        "firstlast",
        "first.last",
    ]
    colleagues = [("Jordan", "Hale"), ("Sam", "Rivera"), ("Kai", "Moreno")]
    rows = []
    for i, c in enumerate(companies[:8]):
        domain = str(c["domain"])
        persona = c["persona"]
        p = dict(persona) if persona else None  # type: ignore[call-overload]
        if p:
            first, last = p["name"].replace("Dr. ", "").split()[0], p["name"].split()[-1]
            rows.append(
                [
                    first,
                    last,
                    p["title"],
                    c["name"],
                    f"https://{domain}",
                    c["location"],
                    c["industry"],
                    c["team_size"],
                    "",
                ]
            )
        if i == 3:
            continue  # no colleague data → verifier can only guess
        pat = patterns[i]
        for cf, cl in colleagues[: 1 + i % 2]:
            local = {
                "first.last": f"{cf}.{cl}",
                "flast": f"{cf[0]}{cl}",
                "first": cf,
                "firstlast": f"{cf}{cl}",
            }[pat].lower()
            rows.append(
                [
                    cf,
                    cl,
                    "Account Manager",
                    c["name"],
                    f"https://{domain}",
                    c["location"],
                    c["industry"],
                    c["team_size"],
                    f"{local}@{domain}",
                ]
            )
    rows.append(
        [
            "Info",
            "Desk",
            "General Inbox",
            "Lumen Ledger",
            "https://lumenledger.test",
            "New York, NY",
            "fintech",
            60,
            "info@lumenledger.test",
        ]
    )
    (ROOT / "examples").mkdir(exist_ok=True)
    with open(ROOT / "examples" / "demo_leads.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "First Name",
                "Last Name",
                "Title",
                "Company",
                "Company Website",
                "Location",
                "Industry",
                "Company Headcount",
                "Email",
            ]
        )
        w.writerows(rows)
    print(f"{len(companies)} companies → {FIX}")


if __name__ == "__main__":
    main()

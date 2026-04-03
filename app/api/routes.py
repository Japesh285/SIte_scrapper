from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.core.site_utils import normalize_site_url
from app.db.database import get_session
from app.db.models import Site
from app.services.orchestrator import orchestrate_scrape

router = APIRouter()


class ScrapeRequest(BaseModel):
    url: str


class ScrapeResponse(BaseModel):
    domain: str
    type: str
    confidence: float
    jobs_found: int
    status: str


class BulkScrapeResponse(BaseModel):
    total_sites: int
    successful: int
    failed: int
    skipped: int
    results: list[ScrapeResponse]


@router.post("/scrape", response_model=ScrapeResponse)
async def scrape(request: ScrapeRequest, session: AsyncSession = Depends(get_session)):
    result = await orchestrate_scrape(request.url, session)
    return ScrapeResponse(
        domain=result["domain"],
        type=result["type"],
        confidence=result["confidence"],
        jobs_found=result["jobs_found"],
        status=result["status"],
    )


HARD_CODED_URLS = [
    "https://medtronic.wd1.myworkdayjobs.com/MedtronicCareers?locationCountry=c4f78be1a8f14da0ab49ce1162348a5e&jobFamilyGroup=2fe8588f35e84eb98ef535f4d738f243",
    "https://medtronic.wd1.myworkdayjobs.com/MedtronicCareers?jobFamilyGroup=5d03e9707876432d93848a9e7146e1ad",
    "https://jobs.dell.com/en/search-jobs/India/375/2/1269750/22/79/50/2",
    "https://jobs.standardchartered.com/go/Experienced-Professional-jobs/9783657/?feedid=363857&markerViewed=&carouselIndex=&facetFilters=%7B%22cust_region%22%3A%5B%22Asia%22%5D%2C%22jobLocationCountry%22%3A%5B%22India%22%5D%2C%22cust_csb_employmentType%22%3A%5B%22+Permanent%22%5D%7D&pageNumber=0",
]


@router.post("/scrape-hardcoded", response_model=BulkScrapeResponse)
async def scrape_hardcoded_urls(session: AsyncSession = Depends(get_session)):
    """Scrape a fixed list of URLs."""

    results = []
    successful = 0
    failed = 0
    skipped = 0

    for url in HARD_CODED_URLS:
        try:
            scrape_result = await orchestrate_scrape(url, session)
            results.append(ScrapeResponse(
                domain=scrape_result["domain"],
                type=scrape_result["type"],
                confidence=scrape_result["confidence"],
                jobs_found=scrape_result["jobs_found"],
                status=scrape_result["status"],
            ))
            if scrape_result["status"] == "success" and scrape_result["jobs_found"] > 0:
                successful += 1
            elif scrape_result["status"] == "skipped":
                skipped += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            results.append(ScrapeResponse(
                domain=url,
                type="ERROR",
                confidence=0.0,
                jobs_found=0,
                status="failed",
            ))

    return BulkScrapeResponse(
        total_sites=len(HARD_CODED_URLS),
        successful=successful,
        failed=failed,
        skipped=skipped,
        results=results,
    )


@router.post("/scrape-all", response_model=BulkScrapeResponse)
async def scrape_all_sites(session: AsyncSession = Depends(get_session)):
    """Scrape all sites from the database one at a time."""

    # Fetch all sites from DB
    result = await session.execute(select(Site))
    sites = result.scalars().all()

    results = []
    successful = 0
    failed = 0
    skipped = 0

    for site in sites:
        try:
            url = normalize_site_url(site.domain)
            scrape_result = await orchestrate_scrape(url, session)
            results.append(ScrapeResponse(
                domain=scrape_result["domain"],
                type=scrape_result["type"],
                confidence=scrape_result["confidence"],
                jobs_found=scrape_result["jobs_found"],
                status=scrape_result["status"],
            ))
            if scrape_result["status"] == "success" and scrape_result["jobs_found"] > 0:
                successful += 1
            elif scrape_result["status"] == "skipped":
                skipped += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            results.append(ScrapeResponse(
                domain=site.domain,
                type="ERROR",
                confidence=0.0,
                jobs_found=0,
                status="failed",
            ))

    return BulkScrapeResponse(
        total_sites=len(sites),
        successful=successful,
        failed=failed,
        skipped=skipped,
        results=results,
    )

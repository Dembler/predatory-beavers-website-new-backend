"""Public HTML reads the live publication pointer on every request."""

import json
from html import escape
from pathlib import Path

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select

from predatory_beavers.api.errors import NotFoundError
from predatory_beavers.modules.editorial.facts import FactSheet
from predatory_beavers.modules.editorial.generation import ColumnDraft, render_paragraphs
from predatory_beavers.modules.editorial.models import Article, ArticleRevision
from predatory_beavers.modules.editorial.service import EditorialService

router = APIRouter(route_class=DishkaRoute, tags=["news"])
_templates = Environment(
    loader=FileSystemLoader(str(Path(__file__).with_name("templates"))),
    autoescape=select_autoescape(("html", "xml")),
)
_headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


async def published(service: EditorialService) -> list[tuple[Article, ArticleRevision]]:
    rows = await service.session.execute(
        select(Article, ArticleRevision)
        .join(
            ArticleRevision,
            ArticleRevision.id == Article.published_revision_id,
        )
        .order_by(Article.published_at.desc())
        .limit(100)
    )
    return [(article, revision) for article, revision in rows]


@router.get("/news", response_class=HTMLResponse)
async def news(request: Request, service: FromDishka[EditorialService]) -> HTMLResponse:
    items = [
        {
            "slug": article.slug,
            "title": revision.content.get("title", "Бобровая неделя"),
            "lead": revision.content.get("lead", ""),
            "published_at": article.published_at,
        }
        for article, revision in await published(service)
    ]
    html = _templates.get_template("news.html").render(
        title="Колонка Бобрика",
        items=items,
        article=None,
        canonical=str(request.url).split("?")[0],
        description="Бобровая неделя: о клубе из первых лап.",
    )
    return HTMLResponse(html, headers=_headers)


@router.get("/news/rss.xml")
async def rss(request: Request, service: FromDishka[EditorialService]) -> Response:
    origin = str(request.base_url).rstrip("/")
    entries = "".join(
        "<item><title>"
        + escape(str(revision.content.get("title", "Бобровая неделя")))
        + "</title><link>"
        + escape(origin + "/news/" + article.slug)
        + "</link><guid>"
        + escape(origin + "/news/" + article.slug)
        + "</guid><description>"
        + escape(str(revision.content.get("lead", "")))
        + "</description></item>"
        for article, revision in await published(service)
    )
    body = '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
    body += "<title>Колонка Бобрика</title><link>" + escape(origin + "/news") + "</link>"
    body += "<description>Бобровая неделя</description>" + entries + "</channel></rss>"
    return Response(body, media_type="application/rss+xml", headers=_headers)


@router.get("/news/{slug}", response_class=HTMLResponse)
async def news_article(
    slug: str,
    request: Request,
    service: FromDishka[EditorialService],
) -> HTMLResponse:
    row = (
        await service.session.execute(
            select(Article, ArticleRevision)
            .join(
                ArticleRevision,
                ArticleRevision.id == Article.published_revision_id,
            )
            .where(Article.slug == slug)
        )
    ).first()
    if row is None:
        raise NotFoundError
    article, revision = row
    draft = ColumnDraft.model_validate(revision.content)
    sheet = FactSheet.model_validate(revision.fact_sheet)
    canonical = str(request.url).split("?")[0]
    structured = (
        json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "Article",
                "headline": draft.title,
                "description": draft.lead,
                "url": canonical,
                "author": {"@type": "Organization", "name": "Бобрик · БК ВГУ «Хищные Бобры»"},
                "datePublished": article.published_at.isoformat() if article.published_at else None,
            },
            ensure_ascii=False,
        )
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    html = _templates.get_template("news.html").render(
        title=draft.title,
        description=draft.lead,
        canonical=canonical,
        items=[],
        article={
            "draft": draft,
            "paragraphs": render_paragraphs(draft, sheet)[1:],
            "sheet": sheet,
            "published_at": article.published_at,
        },
        structured=structured,
    )
    return HTMLResponse(html, headers=_headers)

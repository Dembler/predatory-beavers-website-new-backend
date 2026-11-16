"""Editor-only generation and review; public endpoints only read live publications."""

from uuid import UUID

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from predatory_beavers.api.errors import NotFoundError
from predatory_beavers.api.responses import ApiResponse
from predatory_beavers.modules.club.auth import AdminAuthorizer
from predatory_beavers.modules.editorial.models import Article, EditorialJob
from predatory_beavers.modules.editorial.schemas import EditRequest, GenerateRequest, RevisionAction
from predatory_beavers.modules.editorial.service import EditorialService, job_dto
from predatory_beavers.settings import Settings


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


router = APIRouter(
    route_class=DishkaRoute,
    tags=["editorial"],
    dependencies=[Depends(no_store)],
)


async def authorize(
    request: Request,
    authorizer: AdminAuthorizer,
    settings: Settings,
    *,
    write: bool = False,
) -> None:
    token = request.cookies.get(settings.session_cookie_name)
    if write:
        await authorizer.require_editor(token, request.headers.get(settings.csrf_header_name))
    else:
        await authorizer.require_editor_session(token)


@router.get("/admin/editorial/jobs", response_model=ApiResponse[dict[str, object]])
async def jobs(
    request: Request,
    service: FromDishka[EditorialService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[dict[str, object]]:
    await authorize(request, authorizer, settings)
    rows = await service.session.scalars(
        select(EditorialJob).order_by(EditorialJob.created_at.desc()).limit(100)
    )
    return ApiResponse(message="Editorial jobs", data={"items": [job_dto(row) for row in rows]})


@router.post("/admin/editorial/generate", response_model=ApiResponse[dict[str, object]])
async def generate(
    payload: GenerateRequest,
    request: Request,
    service: FromDishka[EditorialService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[dict[str, object]]:
    await authorize(request, authorizer, settings, write=True)
    key = request.headers.get("Idempotency-Key")
    if not key:
        from predatory_beavers.api.idempotency import InvalidCreationKeyError

        raise InvalidCreationKeyError
    payload.set_creation_key(key)
    return ApiResponse(message="Generation queued", data=job_dto(await service.enqueue(payload)))


@router.get("/admin/editorial/jobs/{job_id}", response_model=ApiResponse[dict[str, object]])
async def get_job(
    job_id: UUID,
    request: Request,
    service: FromDishka[EditorialService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[dict[str, object]]:
    await authorize(request, authorizer, settings)
    return ApiResponse(message="Editorial job", data=job_dto(await service.job(job_id)))


@router.post("/admin/editorial/jobs/{job_id}/cancel", response_model=ApiResponse[dict[str, object]])
async def cancel_job(
    job_id: UUID,
    request: Request,
    service: FromDishka[EditorialService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[dict[str, object]]:
    await authorize(request, authorizer, settings, write=True)
    return ApiResponse(message="Cancellation recorded", data=job_dto(await service.cancel(job_id)))


@router.get("/admin/articles", response_model=ApiResponse[dict[str, object]])
async def admin_articles(
    request: Request,
    service: FromDishka[EditorialService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[dict[str, object]]:
    await authorize(request, authorizer, settings)
    rows = await service.session.scalars(
        select(Article).order_by(Article.created_at.desc()).limit(100)
    )
    return ApiResponse(
        message="Articles",
        data={
            "items": [await service.article_dto(row) for row in rows],
        },
    )


@router.get("/admin/articles/{article_id}", response_model=ApiResponse[dict[str, object]])
async def admin_article(
    article_id: UUID,
    request: Request,
    service: FromDishka[EditorialService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[dict[str, object]]:
    await authorize(request, authorizer, settings)
    return ApiResponse(
        message="Article", data=await service.article_dto(await service.article(article_id))
    )


@router.patch("/admin/articles/{article_id}", response_model=ApiResponse[dict[str, object]])
async def edit_article(
    article_id: UUID,
    payload: EditRequest,
    request: Request,
    service: FromDishka[EditorialService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[dict[str, object]]:
    await authorize(request, authorizer, settings, write=True)
    return ApiResponse(message="Revision saved", data=await service.edit(article_id, payload))


@router.post("/admin/articles/{article_id}/{action}", response_model=ApiResponse[dict[str, object]])
async def article_action(
    article_id: UUID,
    action: str,
    payload: RevisionAction,
    request: Request,
    service: FromDishka[EditorialService],
    authorizer: FromDishka[AdminAuthorizer],
    settings: FromDishka[Settings],
) -> ApiResponse[dict[str, object]]:
    await authorize(request, authorizer, settings, write=True)
    if action not in ("check", "approve", "publish", "unpublish"):
        raise NotFoundError
    return ApiResponse(
        message="Article updated", data=await service.action(article_id, action, payload)
    )


@router.get("/articles", response_model=ApiResponse[dict[str, object]])
async def articles(service: FromDishka[EditorialService]) -> ApiResponse[dict[str, object]]:
    rows = await service.session.scalars(
        select(Article)
        .where(Article.published_revision_id.is_not(None))
        .order_by(Article.published_at.desc())
        .limit(100)
    )
    return ApiResponse(
        message="Published articles",
        data={
            "items": [await service.article_dto(row, public=True) for row in rows],
        },
    )


@router.get("/articles/{slug}", response_model=ApiResponse[dict[str, object]])
async def article(
    slug: str, service: FromDishka[EditorialService]
) -> ApiResponse[dict[str, object]]:
    row = await service.session.scalar(
        select(Article).where(
            Article.slug == slug,
            Article.published_revision_id.is_not(None),
        )
    )
    if row is None:
        raise NotFoundError
    return ApiResponse(
        message="Published article", data=await service.article_dto(row, public=True)
    )

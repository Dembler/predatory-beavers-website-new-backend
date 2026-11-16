"""Transactional editor workflow, independent of model execution."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from predatory_beavers.api.errors import ConflictError, NotFoundError
from predatory_beavers.api.idempotency import CreationLedger
from predatory_beavers.modules.audit.models import AuditAction
from predatory_beavers.modules.audit.service import AuditService
from predatory_beavers.modules.editorial.facts import FactSheet, build_fact_sheet, source_hash
from predatory_beavers.modules.editorial.generation import ColumnDraft, check_draft
from predatory_beavers.modules.editorial.models import (
    Article,
    ArticleCheck,
    ArticleOutbox,
    ArticleRevision,
    EditorialJob,
)
from predatory_beavers.modules.editorial.schemas import (
    EditRequest,
    GenerateRequest,
    RevisionAction,
)
from predatory_beavers.modules.mascot.knowledge import build_corpus


def timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def job_dto(job: EditorialJob) -> dict[str, object]:
    sheet = FactSheet.model_validate(job.fact_sheet)
    return {
        "id": str(job.id),
        "article_id": str(job.article_id),
        "status": job.status,
        "result_revision_id": str(job.result_revision_id) if job.result_revision_id else None,
        "error_code": job.error_code,
        "week_start": sheet.week_start,
        "team_scope": sheet.team_scope,
        "discipline": sheet.discipline,
        "created_at": timestamp(job.created_at),
        "finished_at": timestamp(job.finished_at),
        "provider_attempt_state": job.provider_attempt_state,
        "model": job.model,
        "usage": job.usage,
    }


class EditorialService:
    def __init__(self, session: AsyncSession, audit: AuditService) -> None:
        self.session = session
        self.audit = audit

    async def article(self, article_id: UUID) -> Article:
        article = await self.session.get(Article, article_id, populate_existing=True)
        if article is None:
            raise NotFoundError("Материал не найден")
        return article

    async def revision(self, article: Article, revision_id: UUID) -> ArticleRevision:
        revision = await self.session.get(ArticleRevision, revision_id)
        if revision is None or revision.article_id != article.id:
            raise NotFoundError("Ревизия материала не найдена")
        return revision

    async def latest_check(self, revision_id: UUID) -> ArticleCheck | None:
        row = await self.session.scalar(
            select(ArticleCheck)
            .where(ArticleCheck.revision_id == revision_id)
            .order_by(ArticleCheck.created_at.desc(), ArticleCheck.id.desc())
            .limit(1)
        )
        return row if isinstance(row, ArticleCheck) else None

    async def article_dto(self, article: Article, *, public: bool = False) -> dict[str, object]:
        query = select(ArticleRevision).where(ArticleRevision.article_id == article.id)
        if public:
            query = query.where(ArticleRevision.id == article.published_revision_id)
        revisions = list(await self.session.scalars(query.order_by(ArticleRevision.created_at)))
        items: list[dict[str, object]] = []
        for revision in revisions:
            check = await self.latest_check(revision.id)
            item: dict[str, object] = {
                "id": str(revision.id),
                "content": revision.content,
                "fact_sheet": revision.fact_sheet,
                "created_at": timestamp(revision.created_at),
            }
            if not public:
                item.update(
                    checks=check.report if check else None,
                    check_run_id=str(check.id) if check else None,
                    content_hash=revision.content_hash,
                    model=revision.model,
                    prompt_version=revision.prompt_version,
                    usage=revision.usage,
                    origin=revision.origin,
                )
            items.append(item)
        data: dict[str, object] = {
            "id": str(article.id),
            "slug": article.slug,
            "scope_key": article.scope_key,
            "kind": article.kind,
            "published_revision_id": (
                str(article.published_revision_id) if article.published_revision_id else None
            ),
            "published_at": timestamp(article.published_at),
            "revisions": items,
        }
        if not public:
            data.update(
                version=article.version,
                draft_head_revision_id=(
                    str(article.draft_head_revision_id) if article.draft_head_revision_id else None
                ),
                approved_revision_id=(
                    str(article.approved_revision_id) if article.approved_revision_id else None
                ),
            )
        return data

    async def enqueue(self, payload: GenerateRequest) -> EditorialJob:
        ledger = CreationLedger(self.session)
        previous = await ledger.reserve("editorial_job", payload)
        if previous:
            job = await self.job(previous)
            await self.session.commit()
            return job
        clock = datetime.now(UTC)
        documents = await build_corpus(self.session, now=clock)
        sheet = build_fact_sheet(
            documents,
            payload.week_start,
            team_scope=payload.team_scope,
            discipline=payload.discipline,
            now=clock,
        )
        scope = f"{payload.week_start.isoformat()}-{payload.team_scope}-{payload.discipline}"
        insert = (
            pg_insert if self.session.get_bind().dialect.name == "postgresql" else sqlite_insert
        )
        await self.session.execute(
            insert(Article)
            .values(
                id=uuid4(),
                slug=f"bobrovaya-nedelya-{scope}",
                scope_key=scope,
                kind="weekly_column",
                version=1,
                created_at=clock,
            )
            .on_conflict_do_nothing(index_elements=[Article.scope_key])
        )
        article = await self.session.scalar(select(Article).where(Article.scope_key == scope))
        assert article is not None
        job = EditorialJob(
            article_id=article.id,
            generation_key=str(uuid4()),
            base_head_revision_id=article.draft_head_revision_id,
            fact_sheet=sheet.model_dump(mode="json"),
            brief=payload.brief,
            status="queued",
            deadline_at=clock + timedelta(seconds=120),
        )
        self.session.add(job)
        await self.session.flush()
        await ledger.finish("editorial_job", payload, job.id)
        await self.audit.record(
            action=AuditAction.CREATE,
            entity_type="editorial_job",
            entity_id=job.id,
            after={"article_id": str(article.id), "fact_sheet_fingerprint": sheet.fingerprint},
        )
        await self.session.commit()
        return job

    async def job(self, job_id: UUID) -> EditorialJob:
        job = await self.session.get(EditorialJob, job_id, populate_existing=True)
        if job is None:
            raise NotFoundError("Задача не найдена")
        return job

    async def cancel(self, job_id: UUID) -> EditorialJob:
        job = await self.job(job_id)
        if job.status in ("queued", "running", "cancelling"):
            now = datetime.now(UTC)
            await self.session.execute(
                update(EditorialJob)
                .where(
                    EditorialJob.id == job_id,
                    EditorialJob.status.in_(("queued", "running", "cancelling")),
                )
                .values(
                    cancel_requested_at=now,
                    status=case((EditorialJob.status == "queued", "cancelled"), else_="cancelling"),
                    finished_at=case((EditorialJob.status == "queued", now), else_=None),
                )
            )
            await self.audit.record(
                action=AuditAction.UPDATE,
                entity_type="editorial_job",
                entity_id=job.id,
                after={"action": "cancel"},
            )
            await self.session.commit()
        return await self.job(job_id)

    async def _cas(self, article: Article, version: int, **changes: object) -> None:
        result = await self.session.execute(
            update(Article)
            .where(Article.id == article.id, Article.version == version)
            .values(version=version + 1, **changes)
            .returning(Article.id)
        )
        if result.scalar_one_or_none() is None:
            raise ConflictError("Материал изменился. Обновите страницу перед сохранением.")

    async def _fresh_sheet(self, sheet: FactSheet) -> bool:
        # Compare exact public dependencies at the frozen clock. Unrelated data
        # changes do not invalidate an article, hiding/changing a cited source does.
        documents = await build_corpus(self.session, now=datetime.fromisoformat(sheet.as_of))
        current = {doc.doc_id: source_hash(doc) for doc in documents}
        return all(current.get(source.ref) == source.content_hash for source in sheet.sources)

    async def edit(self, article_id: UUID, payload: EditRequest) -> dict[str, object]:
        article = await self.article(article_id)
        parent = await self.revision(article, payload.revision_id)
        sheet = FactSheet.model_validate(parent.fact_sheet)
        checks = check_draft(payload.content, sheet)
        revision = ArticleRevision(
            id=uuid4(),
            article_id=article.id,
            origin="human",
            content=payload.content.model_dump(mode="json"),
            fact_sheet=parent.fact_sheet,
            content_hash=checks.content_hash,
            model=None,
            prompt_version=parent.prompt_version,
        )
        self.session.add(revision)
        await self.session.flush()
        # Saving does not imply checking/approval. Even an unchanged manual save
        # creates a new revision with no inherited checks.
        await self._cas(
            article,
            payload.expected_version,
            draft_head_revision_id=revision.id,
            approved_revision_id=None,
            approved_check_id=None,
        )
        await self.audit.record(
            action=AuditAction.UPDATE,
            entity_type="article",
            entity_id=article.id,
            after={"action": "edit", "revision_id": str(revision.id)},
        )
        await self.session.commit()
        return await self.article_dto(await self.article(article.id))

    async def action(
        self,
        article_id: UUID,
        action: str,
        payload: RevisionAction,
    ) -> dict[str, object]:
        article = await self.article(article_id)
        revision = await self.revision(article, payload.revision_id)
        sheet = FactSheet.model_validate(revision.fact_sheet)
        draft = ColumnDraft.model_validate(revision.content)
        changes: dict[str, object] = {}
        if action == "check":
            checks = check_draft(draft, sheet)
            if not await self._fresh_sheet(sheet):
                checks = checks.model_copy(
                    update={
                        "passed": False,
                        "issues": [*checks.issues, "stale_sources"],
                    }
                )
            self.session.add(
                ArticleCheck(
                    revision_id=revision.id,
                    report=checks.model_dump(mode="json"),
                )
            )
            changes.update(approved_revision_id=None, approved_check_id=None)
        elif action in ("approve", "publish"):
            check = await self.latest_check(revision.id)
            current = check_draft(draft, sheet)
            if (
                check is None
                or not check.report.get("passed")
                or not current.passed
                or current.content_hash != revision.content_hash
                or check.report.get("content_hash") != revision.content_hash
                or check.report.get("fact_sheet_fingerprint") != sheet.fingerprint
                or check.report.get("policy_version") != current.policy_version
                or not await self._fresh_sheet(sheet)
            ):
                raise ConflictError("Нужна успешная актуальная проверка этой ревизии.")
            if action == "approve":
                changes.update(approved_revision_id=revision.id, approved_check_id=check.id)
            else:
                if (
                    article.approved_revision_id != revision.id
                    or article.approved_check_id != check.id
                ):
                    raise ConflictError("Редактор должен одобрить именно эту проверенную ревизию.")
                changes.update(published_revision_id=revision.id, published_at=datetime.now(UTC))
        elif action == "unpublish":
            if article.published_revision_id != revision.id:
                raise ConflictError("Опубликованная ревизия изменилась.")
            changes.update(published_revision_id=None, published_at=None)
        else:
            raise NotFoundError
        await self._cas(article, payload.expected_version, **changes)
        await self.session.flush()
        if action in ("publish", "unpublish"):
            self.session.add(
                ArticleOutbox(
                    article_id=article.id,
                    revision_id=revision.id,
                    event=action,
                    article_version=payload.expected_version + 1,
                )
            )
        await self.audit.record(
            action=AuditAction.PUBLISH
            if action in ("publish", "unpublish")
            else AuditAction.UPDATE,
            entity_type="article",
            entity_id=article.id,
            after={"action": action, "revision_id": str(revision.id)},
        )
        await self.session.commit()
        return await self.article_dto(await self.article(article.id))

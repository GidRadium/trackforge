"""SQLite repository for renders and output bundles."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trackforge.adapters.persistence.sqlite._errors import translate_integrity_error
from trackforge.adapters.persistence.sqlite._mapping import (
    bundle_from_row,
    render_from_row,
    to_iso_z,
    to_json,
)
from trackforge.adapters.persistence.sqlite.models import (
    OutputBundleRow,
    RenderRow,
)
from trackforge.adapters.persistence.sqlite.repositories._base import SqliteRepository
from trackforge.domain.errors import RecordNotFound
from trackforge.domain.model.ids import ItemId
from trackforge.domain.model.outputs import OutputBundle, Render


class SqliteRenderRepository(SqliteRepository):
    """Persist render operations and their concrete output bundles."""

    async def save_render(self, render: Render) -> Render:
        def _save(session: Session) -> Render:
            row = session.get(RenderRow, render.id)
            if row is None:
                row = RenderRow(
                    id=render.id,
                    item_id=render.item_id,
                    created_at=to_iso_z(render.created_at),
                )
                session.add(row)
            row.item_id = render.item_id
            row.decision_record_id = render.decision_record_id
            row.target_format = render.target_format.value
            row.naming_template = render.naming_template
            row.tag_mapping_version = render.tag_mapping_version
            row.options_json = to_json(render.options)
            row.status = render.status.value
            session.flush()
            return render_from_row(row)

        try:
            return await self._run(_save)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def save_bundle(self, bundle: OutputBundle) -> OutputBundle:
        def _save(session: Session) -> OutputBundle:
            item_id = bundle.item_id
            if item_id is None:
                render = session.get(RenderRow, bundle.render_id)
                if render is None:
                    raise RecordNotFound(
                        "render not found for output bundle",
                        context={"render_id": bundle.render_id},
                    )
                item_id = ItemId(render.item_id)
            row = OutputBundleRow(
                id=bundle.id,
                item_id=item_id,
                render_id=bundle.render_id,
                bundle_hash=bundle.bundle_hash,
                files_json=to_json(list(bundle.files)),
                created_at=to_iso_z(bundle.created_at),
            )
            session.add(row)
            session.flush()
            return bundle_from_row(row)

        try:
            return await self._run(_save)
        except IntegrityError as exc:
            raise translate_integrity_error(exc) from exc

    async def get_bundle(self, bundle_id: str) -> OutputBundle | None:
        def _get(session: Session) -> OutputBundle | None:
            row = session.get(OutputBundleRow, bundle_id)
            return None if row is None else bundle_from_row(row)

        return await self._run(_get)

    async def list_by_item(self, item_id: ItemId) -> list[Render]:
        def _list(session: Session) -> list[Render]:
            rows = session.scalars(
                select(RenderRow)
                .where(RenderRow.item_id == item_id)
                .order_by(RenderRow.created_at, RenderRow.id)
            ).all()
            return [render_from_row(row) for row in rows]

        return await self._run(_list)


__all__ = ["SqliteRenderRepository"]

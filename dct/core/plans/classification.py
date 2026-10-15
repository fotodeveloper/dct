"""Coherent classification writes with manual ownership and observed-input CAS.

Provider work always happens outside write transactions. IDs identify rows;
revision tokens invalidate automatic results after any classification decision.
"""
from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from dct.config import load_config
from dct.core.limits import ensure_fits
from dct.core.plans import classify
from dct.core.plans.crud import VALID_SECTION_TYPES, _validate
from dct.core.projects import resolve_project_id
from dct.db import lock_row, plan_sections, plans, row_to_dict


def manual_owned(row: dict) -> bool:
    return row['classification_source'] == 'manual' and row['section_type'] != 'uncertain'


def _integer_id(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f'{label} must be a positive integer primary key')


def _live_plan(conn, project_slug, plan_id):
    _integer_id(plan_id, 'plan_id')
    project_id = resolve_project_id(conn, project_slug)
    row = conn.execute(sa.select(plans).where(
        plans.c.id == plan_id, plans.c.project_id == project_id,
        plans.c.deleted_at.is_(None),
    )).first()
    if row is None:
        raise ValueError(f'Plan #{plan_id} not found in project')
    return row_to_dict(row)


def _ack(row, changed):
    return {'id': row['id'], 'ulid': row['ulid'],
            'section_type': row['section_type'],
            'classification_source': row['classification_source'], 'changed': changed}


def _values(result):
    _validate(result.section_type, VALID_SECTION_TYPES, 'section_type')
    if result.source not in {'manual', 'rules', 'command'}:
        raise ValueError('Unsupported classification source')
    if result.provider != (None if result.source == 'manual' else result.source):
        raise ValueError('Inconsistent classification provider')
    if result.source in {'manual', 'rules'} and result.model is not None:
        raise ValueError('Only command classification can have a model')
    if result.confidence not in {'high', 'low', 'uncertain'} or (
            result.section_type != 'uncertain' and result.confidence == 'uncertain'):
        raise ValueError('Invalid classification confidence')
    ensure_fits(plan_sections, {'classification_model': result.model})
    uncertain = result.section_type == 'uncertain'
    return {'section_type': result.section_type,
            'section_type_confidence': 'uncertain' if uncertain else result.confidence,
            'needs_llm_review': uncertain, 'classification_source': result.source,
            'classification_provider': result.provider, 'classification_model': result.model}


def _write(conn, row, result, predicates=()):
    """The only classification UPDATE; replay does not stamp time or revision."""
    values = _values(result)
    if all(row[k] == value for k, value in values.items()):
        return False
    now = datetime.now(UTC)
    values.update(classified_at=now, updated_at=now,
                  classification_revision=plan_sections.c.classification_revision + 1)
    return conn.execute(plan_sections.update().where(
        plan_sections.c.id == row['id'], plan_sections.c.deleted_at.is_(None),
        *predicates,
    ).values(**values)).rowcount > 0


def set_plan_section_type(engine, project_slug: str, section_id: int, section_type: str) -> dict:
    """Apply/revoke a manual type; validate live project/parent and preserve identity."""
    _integer_id(section_id, 'section_id')
    _validate(section_type, VALID_SECTION_TYPES, 'section_type')
    with engine.begin() as conn:
        if not lock_row(conn, plan_sections, section_id):
            raise ValueError(f'Plan section #{section_id} not found')
        row = row_to_dict(conn.execute(sa.select(plan_sections).where(
            plan_sections.c.id == section_id)).one())
        _live_plan(conn, project_slug, row['plan_id'])
        changed = _write(conn, row, classify.ClassificationResult(section_type, 'manual'))
        current = row_to_dict(conn.execute(sa.select(plan_sections).where(
            plan_sections.c.id == section_id)).one())
        return _ack(current, changed)


def snapshot_section(engine, project_slug, section_id):
    """Read classification and all stored inference inputs before provider execution."""
    _integer_id(section_id, 'section_id')
    with engine.connect() as conn:
        row = conn.execute(sa.select(plan_sections).where(
            plan_sections.c.id == section_id, plan_sections.c.deleted_at.is_(None))).first()
        if row is None:
            raise ValueError(f'Plan section #{section_id} not found')
        result = row_to_dict(row)
        plan = _live_plan(conn, project_slug, result['plan_id'])
        result['_project_id'] = plan['project_id']
        result['_parent_heading'] = None
        if result['parent_section_id'] is not None:
            parent = conn.execute(sa.select(plan_sections.c.heading).where(
                plan_sections.c.id == result['parent_section_id'],
                plan_sections.c.plan_id == result['plan_id'],
                plan_sections.c.deleted_at.is_(None))).first()
            if parent is None:
                raise ValueError('Live parent section not found in plan')
            result['_parent_heading'] = parent.heading
        return result


def apply_automatic(engine, seen, result, *, uncertain_only=False):
    """CAS a successful result against live identity, revision, and inference inputs."""
    if result.section_type is None or manual_owned(seen):
        return False
    if result.source not in {'rules', 'command'}:
        raise ValueError('Unsupported automatic classification result')
    parent = sa.alias(plan_sections, 'classification_parent')
    predicates = [
        plan_sections.c.plan_id == seen['plan_id'],
        plan_sections.c.ulid == seen['ulid'],
        plan_sections.c.classification_revision == seen['classification_revision'],
        plan_sections.c.heading == seen['heading'],
        plan_sections.c.parent_section_id.is_(None) if seen['parent_section_id'] is None
        else plan_sections.c.parent_section_id == seen['parent_section_id'],
        sa.or_(plan_sections.c.classification_source.is_(None),
               plan_sections.c.classification_source != 'manual',
               plan_sections.c.section_type == 'uncertain'),
        sa.exists(sa.select(plans.c.id).where(
            plans.c.id == seen['plan_id'], plans.c.project_id == seen['_project_id'],
            plans.c.deleted_at.is_(None))),
    ]
    if seen['parent_section_id'] is not None:
        predicates.append(sa.exists(sa.select(parent.c.id).where(
            parent.c.id == seen['parent_section_id'], parent.c.plan_id == seen['plan_id'],
            parent.c.heading == seen['_parent_heading'], parent.c.deleted_at.is_(None))))
    if uncertain_only:
        predicates.append(plan_sections.c.section_type == 'uncertain')
    with engine.begin() as conn:
        return _write(conn, seen, result, predicates)


def _validate_config(config):
    if config.provider not in {'rules', 'command', 'disabled'}:
        raise ValueError('Invalid classification provider')
    if (type(config.timeout_seconds) is not int or config.timeout_seconds <= 0
            or not isinstance(config.model, str)):
        raise ValueError('Invalid classification configuration')
    ensure_fits(plan_sections, {'classification_model': config.model})
    if config.provider == 'command' and (not isinstance(config.command, list)
            or not config.command or not all(isinstance(p, str) and p and '\0' not in p
                                             for p in config.command)):
        raise ValueError('Invalid classification command')
    if config.provider == 'command':
        _, actual_model = classify.command_configuration(config.model, config.command)
        ensure_fits(plan_sections, {'classification_model': actual_model})


def infer(heading, parent_heading, config):
    return classify.classify_result(heading, parent_heading=parent_heading,
        model=config.model, timeout_seconds=config.timeout_seconds,
        provider=config.provider, command=config.command)


def resolve_uncertain_sections(engine, project_slug: str, plan_id: int,
                               classification_config=None) -> dict:
    """Resolve a candidate snapshot and return actual progress, without source writes."""
    config = classification_config if classification_config is not None else load_config().classification
    _validate_config(config)
    with engine.connect() as conn:
        plan = _live_plan(conn, project_slug, plan_id)
        ids = list(conn.execute(sa.select(plan_sections.c.id).where(
            plan_sections.c.plan_id == plan_id, plan_sections.c.deleted_at.is_(None),
            plan_sections.c.section_type == 'uncertain').order_by(
                plan_sections.c.sort_order, plan_sections.c.id)).scalars())
    candidates = []
    skipped = []
    for section_id in ids:
        try:
            candidates.append(snapshot_section(engine, project_slug, section_id))
        except ValueError:
            skipped.append({'id': section_id, 'reason': 'stale_input'})
    resolved, unresolved = [], []
    for seen in candidates:
        if seen['section_type'] != 'uncertain':
            skipped.append({'id': seen['id'], 'reason': 'stale_input'})
            continue
        result = infer(seen['heading'], seen['_parent_heading'], config)
        if result.section_type is None:
            unresolved.append({'id': seen['id'], 'reason': result.reason})
        elif apply_automatic(engine, seen, result, uncertain_only=True):
            resolved.append(seen['id'])
        else:
            skipped.append({'id': seen['id'], 'reason': 'stale_input'})
    with engine.connect() as conn:
        remaining = conn.scalar(sa.select(sa.func.count()).select_from(plan_sections).where(
            plan_sections.c.plan_id == plan_id, plan_sections.c.deleted_at.is_(None),
            plan_sections.c.section_type == 'uncertain'))
    return {'plan_id': plan_id, 'plan_ulid': plan['ulid'], 'resolved': resolved,
            'unresolved': unresolved, 'skipped': skipped, 'remaining_uncertain': remaining}

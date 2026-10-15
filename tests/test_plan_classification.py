"""Manual classification owns a row until revoked, including concurrent inference."""
import pytest

from dct.core.plans.crud import add_plan_section, create_plan, get_plan_section
from dct.core.projects import create_project


def test_manual_type_replay_and_revocation_are_coherent(engine):
    from dct.core.plans.classification import set_plan_section_type
    create_project(engine, slug='sample', name='Sample')
    plan = create_plan(engine, 'sample', 'plan', 'Plan', 'plan')
    section = add_plan_section(engine, plan['id'], 'Unknown topic', 2, section_type='uncertain')
    ack = set_plan_section_type(engine, 'sample', section['id'], 'prose')
    assert ack['changed'] is True
    row = get_plan_section(engine, section['id'])
    assert (row['section_type'], row['classification_source'], row['classification_revision']) == ('prose', 'manual', 1)
    assert not row['needs_llm_review']
    assert set_plan_section_type(engine, 'sample', section['id'], 'prose')['changed'] is False
    set_plan_section_type(engine, 'sample', section['id'], 'uncertain')
    row = get_plan_section(engine, section['id'])
    assert (row['classification_source'], row['classification_revision'], row['needs_llm_review']) == ('manual', 2, True)
    assert set_plan_section_type(engine, 'sample', section['id'], 'uncertain')['changed'] is False


def test_generic_update_cannot_bypass_classification_owner(engine):
    from dct.core.plans.crud import update_plan_section
    create_project(engine, slug='sample', name='Sample')
    plan = create_plan(engine, 'sample', 'plan', 'Plan', 'plan')
    section = add_plan_section(engine, plan['id'], 'Topic', 2)
    with pytest.raises(ValueError, match='classification'):
        update_plan_section(engine, section['id'], section_type='prose')


def _uncertain(engine, heading='Unfamiliar topic'):
    create_project(engine, slug='sample', name='Sample')
    plan = create_plan(engine, 'sample', 'plan', 'Plan', 'plan')
    section = add_plan_section(engine, plan['id'], heading, 2,
        section_type='uncertain', section_type_confidence='uncertain', needs_llm_review=True)
    return plan, section


@pytest.mark.parametrize('bad_id', [True, '1', 0, -1, 1.0])
def test_manual_api_refuses_non_primary_key_without_writes(engine, bad_id):
    from dct.core.plans.classification import set_plan_section_type
    _, section = _uncertain(engine)
    before = get_plan_section(engine, section['id'])
    with pytest.raises(ValueError, match='primary key'):
        set_plan_section_type(engine, 'sample', bad_id, 'prose')
    assert get_plan_section(engine, section['id']) == before


def test_manual_api_guards_project_deleted_parent_and_unknown_type(engine):
    from dct.core.plans.classification import set_plan_section_type
    from dct.core.plans.crud import delete_plan
    plan, section = _uncertain(engine)
    create_project(engine, slug='other', name='Other')
    before = get_plan_section(engine, section['id'])
    with pytest.raises(ValueError):
        set_plan_section_type(engine, 'other', section['id'], 'prose')
    with pytest.raises(ValueError):
        set_plan_section_type(engine, 'sample', section['id'], 'invented')
    delete_plan(engine, plan['id'])
    with pytest.raises(ValueError):
        set_plan_section_type(engine, 'sample', section['id'], 'prose')
    assert get_plan_section(engine, section['id']) == before


@pytest.mark.parametrize('mutation', ['manual', 'aba', 'heading', 'parent_heading', 'delete'])
def test_batch_barrier_rejects_results_after_owner_or_input_changed(concurrent_engine, monkeypatch, mutation):
    """The command barrier proves owner/input changes land during inference, with no write lock held."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from dct.config import ClassificationConfig
    from dct.core.plans import classify
    from dct.core.plans.classification import (
        resolve_uncertain_sections,
        set_plan_section_type,
    )
    from dct.core.plans.crud import delete_plan_section, update_plan_section
    engine = concurrent_engine
    plan, section = _uncertain(engine)
    parent = add_plan_section(engine, plan['id'], 'Parent heading', 1)
    update_plan_section(engine, section['id'], parent_section_id=parent['id'])
    entered, release = Event(), Event()
    def command(*args, **kwargs):
        entered.set()
        assert release.wait(10), 'test did not release inference barrier'
        return type('Result', (), {'returncode': 0, 'stdout': 'reference'})()
    monkeypatch.setattr(classify.shutil, 'which', lambda _: '/fake/command')
    monkeypatch.setattr(classify.subprocess, 'run', command)
    config = ClassificationConfig(provider='command', command=['label-command'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(resolve_uncertain_sections, engine, 'sample', plan['id'], config)
        assert entered.wait(10), 'command was not reached'
        try:
            if mutation in {'manual', 'aba'}:
                set_plan_section_type(engine, 'sample', section['id'], 'prose')
                if mutation == 'aba':
                    set_plan_section_type(engine, 'sample', section['id'], 'uncertain')
            elif mutation == 'heading':
                update_plan_section(engine, section['id'], heading='Changed input')
            elif mutation == 'parent_heading':
                update_plan_section(engine, parent['id'], heading='Changed parent')
            else:
                delete_plan_section(engine, section['id'])
        finally:
            release.set()
        report = future.result(timeout=10)
    assert report['resolved'] == []
    assert report['skipped'] == [{'id': section['id'], 'reason': 'stale_input'}]
    row = get_plan_section(engine, section['id'])
    if row is not None:
        assert row['section_type'] == ('prose' if mutation == 'manual' else 'uncertain')


def test_batch_keeps_progress_reports_failure_and_rerun_only_uncertain(engine, monkeypatch):
    from dct.config import ClassificationConfig
    from dct.core.plans import classify
    from dct.core.plans.classification import resolve_uncertain_sections
    plan, first = _uncertain(engine)
    second = add_plan_section(engine, plan['id'], 'Another unfamiliar topic', 2,
        section_type='uncertain', section_type_confidence='uncertain', needs_llm_review=True)
    commands = []
    monkeypatch.setattr(classify.shutil, 'which', lambda _: '/fake/command')
    def command(argv, **kw):
        commands.append(argv)
        return type('Result', (), {'returncode': 0 if len(commands) == 1 else 1,
                                  'stdout': 'reference'})()
    monkeypatch.setattr(classify.subprocess, 'run', command)
    config = ClassificationConfig(provider='command', command=['label-command'])
    report = resolve_uncertain_sections(engine, 'sample', plan['id'], config)
    assert report['resolved'] == [first['id']]
    assert report['unresolved'] == [{'id': second['id'], 'reason': 'command_failed'}]
    assert report['remaining_uncertain'] == 1
    successful = get_plan_section(engine, first['id'])
    assert (successful['classification_source'], successful['classification_provider'],
            successful['classification_model']) == ('command', 'command', None)
    resolve_uncertain_sections(engine, 'sample', plan['id'], config)
    assert len(commands) == 3
    assert get_plan_section(engine, first['id']) == successful


def test_rules_under_command_provider_record_rules_without_external_invocation(engine, monkeypatch):
    from dct.config import ClassificationConfig
    from dct.core.plans import classify
    from dct.core.plans.classification import resolve_uncertain_sections
    plan, section = _uncertain(engine, 'Sprint 1')
    def forbidden(*args, **kwargs):
        pytest.fail('rule hit invoked command')
    monkeypatch.setattr(classify.subprocess, 'run', forbidden)
    report = resolve_uncertain_sections(engine, 'sample', plan['id'], ClassificationConfig(provider='command'))
    assert report['resolved'] == [section['id']]
    row = get_plan_section(engine, section['id'])
    assert (row['classification_source'], row['classification_provider'], row['classification_model']) == ('rules', 'rules', None)


def test_three_bootstraps_preserve_legacy_unknown_provenance_without_repeated_writes(engine, capsys):
    """Upgrade adds each column once and never guesses old decision history."""
    import sqlalchemy as sa

    from dct.db import create_tables
    _, section = _uncertain(engine)
    fields = ('classification_source', 'classification_provider', 'classification_model',
              'classified_at', 'classification_revision')
    # Isolated fixture schema: remove only this migration's columns to emulate legacy DB.
    with engine.begin() as conn:
        for field in fields:
            conn.execute(sa.text(f'ALTER TABLE plan_sections DROP COLUMN {field}'))
    writes = []
    changed_rows = []
    def observe(conn, cursor, statement, parameters, context, executemany):
        operation = statement.lstrip().split()[0].upper()
        if operation in {'UPDATE', 'INSERT', 'DELETE', 'ALTER', 'CREATE', 'DROP'}:
            writes.append(statement)
        if operation in {'UPDATE', 'INSERT', 'DELETE'} and cursor.rowcount > 0:
            changed_rows.append(cursor.rowcount)
    sa.event.listen(engine, 'after_cursor_execute', observe)
    try:
        create_tables(engine)
        assert len([s for s in writes if 'ALTER TABLE plan_sections ADD COLUMN' in s]) == 5
        writes.clear()
        changed_rows.clear()
        capsys.readouterr()
        for _ in range(2):
            create_tables(engine)
            assert not [s for s in writes if 'plan_sections' in s]
            assert changed_rows == []
            assert capsys.readouterr().err == ''
            writes.clear()
    finally:
        sa.event.remove(engine, 'after_cursor_execute', observe)
    row = get_plan_section(engine, section['id'])
    assert [row[k] for k in fields] == [None, None, None, None, 0]


@pytest.mark.parametrize('provider', ['rules', 'command'])
def test_ingest_preserves_manual_type_through_heading_change_until_revoked(engine, tmp_path, monkeypatch, provider):
    from dct.config import Config, save_config
    from dct.core.plans import classify
    from dct.core.plans.classification import set_plan_section_type
    from dct.core.plans.crud import list_plan_sections
    from dct.core.plans.ingest import ingest_file
    create_project(engine, slug='sample', name='Sample', path=str(tmp_path))
    path = tmp_path / 'plan.md'
    path.write_text('# 1. Plan\n\n## Unfamiliar topic\n')
    cfg = Config(database_url='sqlite:///unused.db')
    cfg.classification.provider = provider
    cfg.classification.command = ['label-command']
    cfg_path = tmp_path / 'config.toml'
    save_config(cfg, cfg_path)
    monkeypatch.setenv('DCT_CONFIG_PATH', str(cfg_path))
    report = ingest_file(engine, path, 'sample', auto_resolve_uncertain=False)
    section = list_plan_sections(engine, report['plan_id'])[1]
    set_plan_section_type(engine, 'sample', section['id'], 'prose')
    manual = get_plan_section(engine, section['id'])
    original = path.read_text()
    # Deterministic parser positive inference must not erase manual ownership.
    path.write_text(original.replace('Unfamiliar topic', 'Sprint 1'))
    def forbidden(*args, **kwargs):
        pytest.fail('manual section invoked inference')
    monkeypatch.setattr(classify.subprocess, 'run', forbidden)
    ingest_file(engine, path, 'sample')
    current = get_plan_section(engine, section['id'])
    assert current['heading'] == 'Sprint 1'
    assert (current['section_type'], current['classification_revision'], current['classified_at']) == ('prose', manual['classification_revision'], manual['classified_at'])
    assert current['id'] == section['id'] and current['ulid'] == section['ulid']
    # Unknown heading also cannot invoke an enabled external command while owned.
    path.write_text(original.replace('Unfamiliar topic', 'Another unfamiliar topic'))
    ingest_file(engine, path, 'sample')
    assert get_plan_section(engine, section['id'])['section_type'] == 'prose'
    set_plan_section_type(engine, 'sample', section['id'], 'uncertain')
    path.write_text(original.replace('Unfamiliar topic', 'Sprint 2'))
    ingest_file(engine, path, 'sample')
    current = get_plan_section(engine, section['id'])
    assert (current['section_type'], current['classification_source'], current['classification_revision']) == ('sprint', 'rules', 3)


def test_batch_and_manual_resolution_preserve_source_file_bytes(engine, tmp_path):
    from dct.config import ClassificationConfig
    from dct.core.plans.classification import (
        resolve_uncertain_sections,
        set_plan_section_type,
    )
    from dct.core.plans.crud import update_plan
    plan, section = _uncertain(engine, 'Sprint 1')
    path = tmp_path / 'plan.md'
    path.write_bytes(b'# Sprint 1\r\nunchanged prose\r\n')
    update_plan(engine, plan['id'], source_file=str(path))
    before = path.read_bytes()
    resolve_uncertain_sections(engine, 'sample', plan['id'], ClassificationConfig())
    set_plan_section_type(engine, 'sample', section['id'], 'prose')
    assert path.read_bytes() == before


@pytest.mark.parametrize('config_change', [('provider', 'unknown'), ('command', []), ('timeout_seconds', 0), ('model', 'x' * 201)])
def test_invalid_batch_config_refuses_before_invocation_or_writes(engine, monkeypatch, config_change):
    from dct.config import ClassificationConfig
    from dct.core.plans import classify
    from dct.core.plans.classification import resolve_uncertain_sections
    plan, section = _uncertain(engine)
    before = get_plan_section(engine, section['id'])
    cfg = ClassificationConfig(provider='command')
    setattr(cfg, *config_change)
    def forbidden(*args, **kwargs):
        pytest.fail('invalid config invoked a command')
    monkeypatch.setattr(classify.subprocess, 'run', forbidden)
    with pytest.raises(ValueError):
        resolve_uncertain_sections(engine, 'sample', plan['id'], cfg)
    assert get_plan_section(engine, section['id']) == before


def test_disabled_batch_reports_unresolved_without_attempt_or_change(engine, monkeypatch):
    from dct.config import ClassificationConfig
    from dct.core.plans import classify
    from dct.core.plans.classification import resolve_uncertain_sections
    plan, section = _uncertain(engine)
    before = get_plan_section(engine, section['id'])
    monkeypatch.setattr(classify.subprocess, 'run', lambda *a, **k: pytest.fail('disabled invoked command'))
    report = resolve_uncertain_sections(engine, 'sample', plan['id'], ClassificationConfig(provider='disabled'))
    assert report['unresolved'] == [{'id': section['id'], 'reason': 'disabled'}]
    assert report['remaining_uncertain'] == 1
    assert get_plan_section(engine, section['id']) == before


def test_ingest_command_barrier_cannot_overwrite_concurrent_manual_decision(concurrent_engine, tmp_path, monkeypatch):
    """Ingest snapshots before provider work and performs the same revision CAS as batch."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from dct.config import Config, save_config
    from dct.core.plans import classify
    from dct.core.plans.classification import set_plan_section_type
    from dct.core.plans.crud import list_plan_sections
    from dct.core.plans.ingest import ingest_file
    engine = concurrent_engine
    create_project(engine, slug='sample', name='Sample', path=str(tmp_path))
    cfg = Config(database_url='sqlite:///unused.db')
    cfg.classification.provider = 'command'
    cfg.classification.command = ['label-command']
    cfg_path = tmp_path / 'config.toml'
    save_config(cfg, cfg_path)
    monkeypatch.setenv('DCT_CONFIG_PATH', str(cfg_path))
    path = tmp_path / 'plan.md'
    path.write_text('# 1. Plan\n\n## Unfamiliar topic\n')
    report = ingest_file(engine, path, 'sample', auto_resolve_uncertain=False)
    section = list_plan_sections(engine, report['plan_id'])[1]
    entered, release = Event(), Event()
    def command(*args, **kwargs):
        entered.set()
        assert release.wait(10), 'test did not release ingest inference barrier'
        return type('Result', (), {'returncode': 0, 'stdout': 'reference'})()
    monkeypatch.setattr(classify.shutil, 'which', lambda _: '/fake/command')
    monkeypatch.setattr(classify.subprocess, 'run', command)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ingest_file, engine, path, 'sample')
        assert entered.wait(10), 'ingest did not reach command'
        try:
            set_plan_section_type(engine, 'sample', section['id'], 'prose')
        finally:
            release.set()
        report = future.result(timeout=10)
    row = get_plan_section(engine, section['id'])
    assert (row['section_type'], row['classification_source'], row['classification_revision']) == ('prose', 'manual', 1)
    assert report['uncertain_resolved'] == 0


@pytest.mark.parametrize('heading, command_output, expected_type, expected_source', [
    ('Sprint 1', None, 'sprint', 'rules'),
    ('Unfamiliar topic', 'reference', 'reference', 'command'),
    ('Unfamiliar topic', '', 'uncertain', 'rules'),
])
def test_ingest_insert_contains_complete_actual_classification_at_revision_zero(engine, tmp_path, monkeypatch, heading, command_output, expected_type, expected_source):
    """Observe INSERT itself: a later corrective UPDATE cannot make this test pass."""
    import sqlalchemy as sa

    from dct.config import Config, save_config
    from dct.core.plans import classify
    from dct.core.plans.ingest import ingest_file
    from dct.db import plan_sections, row_to_dict

    create_project(engine, slug='sample', name='Sample', path=str(tmp_path))
    cfg = Config(database_url='sqlite:///unused.db')
    cfg.classification.provider = 'command' if command_output is not None else 'rules'
    cfg.classification.command = ['label-command']
    cfg_path = tmp_path / 'config.toml'
    save_config(cfg, cfg_path)
    monkeypatch.setenv('DCT_CONFIG_PATH', str(cfg_path))
    monkeypatch.setattr(classify.shutil, 'which', lambda _: '/fake/command')
    monkeypatch.setattr(classify.subprocess, 'run', lambda *a, **k:
                        type('Result', (), {'returncode': 0, 'stdout': command_output})())
    path = tmp_path / 'plan.md'
    path.write_text(f'# {heading}\n')
    inserted = []
    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith('INSERT INTO plan_sections '):
            inserted.extend(row_to_dict(r) for r in conn.execute(sa.select(plan_sections)))
    sa.event.listen(engine, 'after_cursor_execute', observe)
    try:
        ingest_file(engine, path, 'sample')
    finally:
        sa.event.remove(engine, 'after_cursor_execute', observe)
    assert len(inserted) == 1
    row = inserted[0]
    assert (row['section_type'], row['classification_source'], row['classification_provider'],
            row['classification_revision']) == (expected_type, expected_source, expected_source, 0)
    assert row['classification_model'] is None and row['classified_at'] is not None
    assert (row['section_type_confidence'], row['needs_llm_review']) == (
        'uncertain' if expected_type == 'uncertain' else 'high', expected_type == 'uncertain')


@pytest.mark.parametrize('model_args', [['--model', 'x' * 201], ['--model=' + 'x' * 201]])
def test_argv_model_limit_refuses_batch_before_command_or_partial_progress(engine, monkeypatch, model_args):
    """The effective argv model is config input, refused before any candidate writes."""
    from dct.config import ClassificationConfig
    from dct.core.plans import classify
    from dct.core.plans.classification import resolve_uncertain_sections

    plan, first = _uncertain(engine, 'Sprint 1')
    second = add_plan_section(engine, plan['id'], 'Unfamiliar topic', 2, section_type='uncertain')
    before = [get_plan_section(engine, s['id']) for s in (first, second)]
    called = []
    monkeypatch.setattr(classify.shutil, 'which', lambda cmd: cmd)
    monkeypatch.setattr(classify.subprocess, 'run', lambda *a, **k:
                        called.append(a) or type('Result', (), {'returncode': 0, 'stdout': 'reference'})())
    cfg = ClassificationConfig(provider='command', command=['claude', '--print', *model_args])
    with pytest.raises(ValueError, match='classification_model too long'):
        resolve_uncertain_sections(engine, 'sample', plan['id'], cfg)
    assert called == []
    assert [get_plan_section(engine, s['id']) for s in (first, second)] == before


def test_argv_model_limit_refuses_ingest_before_file_or_database_effects(engine, tmp_path, monkeypatch):
    from dct.config import Config, save_config
    from dct.core.plans import classify
    from dct.core.plans.crud import list_plans
    from dct.core.plans.ingest import ingest_file

    create_project(engine, slug='sample', name='Sample', path=str(tmp_path))
    cfg = Config(database_url='sqlite:///unused.db')
    cfg.classification.provider = 'command'
    cfg.classification.command = ['claude', '--print', '--model=' + 'x' * 201]
    cfg_path = tmp_path / 'config.toml'
    save_config(cfg, cfg_path)
    monkeypatch.setenv('DCT_CONFIG_PATH', str(cfg_path))
    path = tmp_path / 'plan.md'
    path.write_text('# 1. Plan\n\n## Unfamiliar topic\n')
    original = path.read_bytes()
    monkeypatch.setattr(classify.shutil, 'which', lambda cmd: cmd)
    monkeypatch.setattr(classify.subprocess, 'run', lambda *a, **k:
                        type('Result', (), {'returncode': 0, 'stdout': 'reference'})())
    with pytest.raises(ValueError, match='classification_model too long'):
        ingest_file(engine, path, 'sample')
    assert path.read_bytes() == original
    assert list_plans(engine, 'sample') == []


@pytest.mark.parametrize('anchor_kind', ['section', 'cp'])
@pytest.mark.parametrize('existing_receiver', [False, True])
def test_foreign_anchor_refused_before_any_ingest_effect(engine, tmp_path, anchor_kind, existing_receiver):
    """A late foreign anchor cannot leave earlier section/hash/anchor writes behind."""
    from dct.core.plans.crud import add_plan_checkpoint, get_plan, list_plans
    from dct.core.plans.ingest import ingest_file

    create_project(engine, slug='sample', name='Sample', path=str(tmp_path))
    donor = create_plan(engine, 'sample', 'donor', 'Donor', 'plan')
    section = add_plan_section(engine, donor['id'], 'Sprint 1', 2)
    checkpoint = add_plan_checkpoint(engine, section['id'], 'Donor checkpoint')
    path = tmp_path / 'receiver.md'
    receiver = create_plan(engine, 'sample', 'receiver', 'Receiver', 'plan',
                           source_file=str(path), source_hash='prior-hash') if existing_receiver else None
    prefix = '<!-- dct:plan:' + receiver['ulid'] + ' -->\n' if receiver else ''
    if anchor_kind == 'section':
        foreign = '## Sprint 2\n<!-- dct:section:' + section['ulid'] + ' -->\n'
    else:
        foreign = '- [ ] <!-- dct:cp:' + checkpoint['ulid'] + ' --> Copied checkbox\n'
    path.write_text(prefix + '# 1. Receiver\n\n' + foreign)
    before_file = path.read_bytes()
    before_db = [get_plan(engine, p['id']) for p in list_plans(engine, 'sample')]
    with pytest.raises(ValueError, match='anchor.*another plan'):
        ingest_file(engine, path, 'sample', auto_resolve_uncertain=False)
    assert path.read_bytes() == before_file
    assert [get_plan(engine, p['id']) for p in list_plans(engine, 'sample')] == before_db


@pytest.mark.parametrize('executable,model_length,expected_model', [('claude', 200, 'x' * 200), ('custom-classifier', 201, None)])
def test_valid_boundary_or_unknown_command_model_preserves_actual_provenance(engine, monkeypatch, executable, model_length, expected_model):
    """Accept the storage boundary and never invent a model for a custom argv."""
    from dct.config import ClassificationConfig
    from dct.core.plans import classify
    from dct.core.plans.classification import resolve_uncertain_sections

    plan, section = _uncertain(engine)
    monkeypatch.setattr(classify.shutil, 'which', lambda cmd: cmd)
    monkeypatch.setattr(classify.subprocess, 'run', lambda *a, **k:
                        type('Result', (), {'returncode': 0, 'stdout': 'reference'})())
    cfg = ClassificationConfig(provider='command', command=[executable, '--model', 'x' * model_length])
    report = resolve_uncertain_sections(engine, 'sample', plan['id'], cfg)
    assert report['resolved'] == [section['id']]
    assert report['remaining_uncertain'] == 0
    assert get_plan_section(engine, section['id'])['classification_model'] == expected_model


@pytest.mark.parametrize('explicit_flag,config_default', [(False, True), (None, False)])
def test_disabled_ingest_ignores_unused_invalid_command_configuration(engine, tmp_path, monkeypatch, explicit_flag, config_default):
    """Explicit false and default false preserve structural ingestion without invoking a provider."""
    from dct.config import Config, save_config
    from dct.core.plans import classify
    from dct.core.plans.crud import list_plan_sections
    from dct.core.plans.ingest import ingest_file

    create_project(engine, slug='sample', name='Sample', path=str(tmp_path))
    cfg = Config(database_url='sqlite:///unused.db')
    cfg.plans.auto_resolve_uncertain = config_default
    cfg.classification.provider = 'command'
    cfg.classification.command = ['claude', '--print', '--model=' + 'x' * 201]
    cfg_path = tmp_path / 'config.toml'
    save_config(cfg, cfg_path)
    monkeypatch.setenv('DCT_CONFIG_PATH', str(cfg_path))
    def forbidden(*args, **kwargs):
        pytest.fail('disabled classification consulted the provider')
    monkeypatch.setattr(classify.shutil, 'which', forbidden)
    monkeypatch.setattr(classify.subprocess, 'run', forbidden)
    path = tmp_path / 'plan.md'
    path.write_text('# 1. Plan\n\n## Unfamiliar topic\n')
    report = ingest_file(engine, path, 'sample', auto_resolve_uncertain=explicit_flag)
    assert (report['sections_created'], report['uncertain_resolved'], report['uncertain_remaining']) == (2, 0, 1)
    rows = list_plan_sections(engine, report['plan_id'])
    assert [(r['section_type'], r['classification_source']) for r in rows] == [('prose', 'rules'), ('uncertain', 'rules')]
    assert '<!-- dct:plan:' in path.read_text()

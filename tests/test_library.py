import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app import create_app
from backend.library import Library
from backend.manager import ACTIVE
from tests.fake_colab import PROFILES, FakeColab, template


@pytest.fixture
async def library(tmp_path):
    app = create_app(tmp_path, driver=FakeColab(), account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            client.headers['x-workbench-token'] = (await client.get('/api/state')).json()['token']
            yield client, app.state.manager, Library(app.state.manager)


def artwork(manager, account='colab2', status='completed'):
    key = uuid.uuid4().hex
    folder = manager.store.directory / 'outputs' / key
    folder.mkdir(parents=True)
    (folder / 'video.mp4').write_bytes(b'saved artwork')
    (folder / 'workflow-api.json').write_text('{}')
    manager.store.put('jobs', key, {'id': key, 'name': 'Original', 'account': account, 'status': status,
                                  'created_at': time.time(), 'graph': template(), 'outputs': [
                                      {'filename': 'video.mp4', 'bytes': 13, 'url': f'/api/jobs/{key}/files/video.mp4'}]})
    return key, folder


async def test_trash_restore_rename_preserve_files_draft_and_other_account(library):
    client, manager, _ = library
    key, folder = artwork(manager)
    other, other_folder = artwork(manager, 'colab3')
    draft = {'id': 'main', 'name': 'Draft', 'graph': template(), 'positions': {}}
    manager.store.put('drafts', 'main', draft)
    assert (await client.patch(f'/api/jobs/{key}', json={'name': ' New title '})).json()['name'] == 'New title'
    assert (await client.patch(f'/api/jobs/{key}', json={'name': '  '})).status_code == 400
    response = await client.post('/api/artworks/manage', json={'ids': [key], 'action': 'trash'})
    assert response.json() == {'ids': [key], 'failed': []}
    assert manager.job(key)['deleted_at']
    assert folder.joinpath('video.mp4').read_bytes() == b'saved artwork'
    assert (await client.get(f'/api/jobs/{key}/files/workflow-api.json')).status_code == 200
    assert (await client.patch(f'/api/jobs/{key}', json={'name': 'No'})).status_code == 400
    assert (await client.post('/api/artworks/manage', json={'ids': [key], 'action': 'restore'})).status_code == 200
    assert manager.job(key)['deleted_at'] is None
    assert manager.job(other).get('deleted_at') is None
    assert other_folder.exists()
    assert manager.store.get('drafts', 'main') == draft
    assert manager.colab.calls == []


@pytest.mark.parametrize('status', sorted(ACTIVE))
async def test_mixed_batch_rejects_active_jobs_before_any_mutation(library, status):
    client, manager, _ = library
    completed, _ = artwork(manager)
    active, _ = artwork(manager, status=status)
    response = await client.post('/api/artworks/manage', json={'ids': [completed, active], 'action': 'trash'})
    assert response.status_code == 400
    assert manager.job(completed).get('deleted_at') is None
    manager.job(active, status='cancelled')  # No actual remote job was started.


async def test_batch_delete_only_trashed_and_selected_local_files(library):
    client, manager, _ = library
    key, folder = artwork(manager)
    other, other_folder = artwork(manager, 'colab3')
    assert (await client.post('/api/artworks/manage', json={'ids': [key], 'action': 'delete'})).status_code == 400
    assert (await client.post('/api/artworks/manage', json={'ids': [key, 'missing'], 'action': 'trash'})).status_code == 400
    assert manager.job(key).get('deleted_at') is None
    await client.post('/api/artworks/manage', json={'ids': [key, key], 'action': 'trash'})
    response = await client.post('/api/artworks/manage', json={'ids': [key], 'action': 'delete'})
    assert response.json()['ids'] == [key]
    assert not folder.exists() and manager.job(key) is None
    assert other_folder.exists() and manager.job(other)
    assert (await client.get(f'/api/jobs/{key}/files/video.mp4')).status_code == 404
    assert manager.colab.calls == []


async def test_symlink_directory_cannot_delete_external_files(library, tmp_path):
    _, manager, library = library
    key, folder = artwork(manager)
    library.manage([key], 'trash')
    external = tmp_path / 'keep'
    folder.rename(external)
    folder.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match='路径异常'):
        library.manage([key], 'delete')
    assert (external / 'video.mp4').is_file()
    assert manager.job(key)


async def test_library_mutations_require_csrf(library):
    client, manager, _ = library
    key, _ = artwork(manager)
    response = await client.post('/api/artworks/manage', json={'ids': [key], 'action': 'trash'},
                                 headers={'x-workbench-token': 'invalid'})
    assert response.status_code == 403
    assert manager.job(key).get('deleted_at') is None


async def test_permanent_delete_waits_for_cancelled_worker_to_finish(library):
    _, manager, library = library
    key, folder = artwork(manager, status='cancelled')
    library.manage([key], 'trash')
    manager.inflight_jobs.add(key)
    with pytest.raises(ValueError, match='收尾'):
        library.manage([key], 'delete')
    assert folder.exists()
    manager.inflight_jobs.remove(key)
    assert library.manage([key], 'delete')['ids'] == [key]


async def test_partial_filesystem_failure_is_reported_per_item(library, monkeypatch):
    _, manager, library = library
    first, _ = artwork(manager)
    second, second_folder = artwork(manager)
    library.manage([first, second], 'trash')
    import shutil
    original = shutil.rmtree

    def remove(path):
        if path == second_folder:
            raise PermissionError('denied')
        original(path)

    monkeypatch.setattr('backend.library.shutil.rmtree', remove)
    result = library.manage([first, second], 'delete')
    assert result['ids'] == [first]
    assert result['failed'][0]['id'] == second
    assert manager.job(second)['deleted_at'] and second_folder.exists()

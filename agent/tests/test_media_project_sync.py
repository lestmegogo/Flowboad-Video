import pytest


@pytest.mark.asyncio
async def test_identity_mapping_skips_reupload(monkeypatch):
    from flowboard.services import media_project_sync as sync_service

    media_id = "11111111-2222-3333-4444-555555555555"
    project_id = "project-123"
    sync_service.record_media_project_identity([media_id], project_id)

    class UnexpectedSdk:
        async def upload_image(self, **_kwargs):
            raise AssertionError("identity mapping should skip upload")

    monkeypatch.setattr(sync_service, "get_flow_sdk", lambda: UnexpectedSdk())

    synced = await sync_service.ensure_media_in_project(media_id, project_id)

    assert synced == media_id

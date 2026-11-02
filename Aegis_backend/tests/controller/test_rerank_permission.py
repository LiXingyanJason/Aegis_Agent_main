from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from uuid import uuid4
import pytest
from pydantic import ValidationError
from fastapi import HTTPException
from app.param.rerank_param import RerankPermissionParam
from app.service.rerank_permission_service import RerankPermissionService


@pytest.mark.parametrize('data',[dict(allowed='true'),dict(allowed=True,tenant_id='fake')])
def test_permission_contract_is_explicit_and_no_identity_fields(data):
    with pytest.raises(ValidationError): RerankPermissionParam(**data)


@pytest.mark.asyncio
async def test_owner_update_and_not_found():
    user=SimpleNamespace(id=uuid4(),tenant_id=uuid4());doc=uuid4()
    with patch('app.service.rerank_permission_service.KnowledgeRepository') as repo:
        repo.return_value.set_rerank_permission=AsyncMock(return_value=False)
        with pytest.raises(HTTPException) as error:await RerankPermissionService().change(None,user,doc,True)
        assert error.value.status_code==404
        repo.return_value.set_rerank_permission.return_value=True
        assert (await RerankPermissionService().change(None,user,doc,False))['external_rerank_allowed'] is False

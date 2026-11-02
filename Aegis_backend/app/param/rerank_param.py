from pydantic import BaseModel,ConfigDict,StrictBool


class RerankPermissionParam(BaseModel):
    model_config=ConfigDict(extra='forbid')
    allowed: StrictBool

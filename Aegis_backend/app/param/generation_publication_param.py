from uuid import UUID
from pydantic import BaseModel,ConfigDict,Field,StrictBool


class GenerationPublicationParam(BaseModel):
    model_config=ConfigDict(extra='forbid')
    version_id: UUID
    generation_id: UUID | None = None
    expected_revision: int = Field(strict=True,ge=0)
    review_reference: str = Field(min_length=1,max_length=200)
    rollback: StrictBool = False

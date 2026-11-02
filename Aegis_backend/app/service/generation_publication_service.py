from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError
from app.repository.generation_publication_repository import GenerationPublicationRepository


class GenerationPublicationService:
    def __init__(self,repository=None):self.repo=repository or GenerationPublicationRepository()
    async def publish(self,session,user,document_id,param):
        if not param.review_reference.strip() or (param.generation_id is None and not param.rollback):
            raise HTTPException(422,detail={'code':'GENERATION_PUBLICATION_REJECTED'})
        try:return dict(await self.repo.publish(session,user,document_id,param))
        except DBAPIError as error:
            # Never return the SQL, bound values or arbitrary database diagnostics.
            known=('GENERATION_REVISION_CONFLICT','GENERATION_NOT_READY','GENERATION_NOT_PREVIOUS','SHADOW_SOURCE_UNAVAILABLE')
            code=next((c for c in known if c in str(error.orig)),'GENERATION_PUBLICATION_REJECTED')
            raise HTTPException(409,detail={'code':code}) from None
    async def status(self,session,user,document_id):
        row=await self.repo.status(session,user,document_id)
        if row is None:raise HTTPException(404,detail={'code':'KNOWLEDGE_DOCUMENT_NOT_FOUND'})
        return row

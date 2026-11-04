from app.repository.memory_formation_repository import MemoryFormationRepository


class MemoryFormationService:
    def __init__(self,repository=None):self.repository=repository or MemoryFormationRepository()
    def capabilities(self,value):
        return {**value,'extraction_available':False,'auto_approval_available':False}
    async def read(self,session,user):return self.capabilities(await self.repository.read(session,user))
    async def update(self,session,user,param):return self.capabilities(await self.repository.update(session,user,param))

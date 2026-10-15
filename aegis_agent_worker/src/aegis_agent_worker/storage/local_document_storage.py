"""只允许服务器生成的UUID key，文件保存在共享根目录下，不覆盖已有对象。"""
import asyncio
from pathlib import Path
from uuid import UUID


class LocalDocumentStorage:
    def __init__(self, root, max_bytes=262144):
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes

    def _path(self, key):
        if str(UUID(key)) != key:
            raise ValueError("非法源文件key")
        path = self.root / key
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError("源文件路径越界")
        return path

    async def put(self, key, content):
        if not content or len(content) > self.max_bytes:
            raise ValueError("源文件大小超限")
        def write():
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(key)
            try:
                with path.open('xb') as stream:
                    stream.write(content)
            except FileExistsError:
                raise ValueError("不能覆盖源文件") from None
        await asyncio.to_thread(write)

    async def read(self, key):
        def read():
            with self._path(key).open('rb') as stream:
                result = stream.read(self.max_bytes+1)
            if len(result) > self.max_bytes:
                raise ValueError("源文件大小超限")
            return result
        return await asyncio.to_thread(read)

    async def delete(self, key):
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)

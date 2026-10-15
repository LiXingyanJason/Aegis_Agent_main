"""显式源文件补偿清理，默认dry-run；只处理至少24小时前生成且无SQL引用的UUID对象。"""
import argparse
import asyncio
import json
from pathlib import Path
from time import time
from uuid import UUID
from sqlalchemy import text
from aegis_agent_worker.config.database import Database
from aegis_agent_worker.config.settings import get_settings
from aegis_agent_worker.storage.local_document_storage import LocalDocumentStorage


async def main(apply=False):
    settings=get_settings()
    root=Path(settings.knowledge_storage_root).resolve()
    if root in {Path.home().resolve(),Path(__file__).resolve().parents[2],Path(root.anchor)}:
        raise ValueError('禁止将宽泛目录作为源文件清理根目录')
    storage=LocalDocumentStorage(root,settings.knowledge_max_source_bytes)
    database=Database(settings)
    eligible=removed=0
    try:
        for path in list(root.iterdir())[:1000] if root.exists() else []:
            if eligible>=100: break
            try:
                key=str(UUID(path.name))
                if key!=path.name or path.is_symlink() or not path.is_file() or path.stat().st_mtime>time()-86400:
                    continue
            except ValueError: continue
            # 管理员维护连接必须能看到所有租户引用；不能用用户级RLS结果判断文件是否孤立。
            async with database.session() as session:
                async with session.begin():
                    role=(await session.execute(text('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user'))).first()
                    if not role or not (role[0] or role[1]):
                        raise ValueError('清理需要能核验全租户引用的维护连接')
                    referenced=(await session.execute(text("SELECT 1 FROM knowledge_document_versions WHERE storage_backend='local' AND storage_key=:key"),dict(key=key))).first()
                    if referenced: continue
                    eligible+=1
                    if apply:
                        await storage.delete(key)
                        removed+=1
        print(json.dumps(dict(dry_run=not apply,eligible=eligible,removed=removed)))
    finally:
        await database.dispose()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true',help='执行删除，默认仅统计；删除前请备份')
    args=parser.parse_args()
    try:
        asyncio.run(main(args.apply))
    except Exception:
        print('源文件清理未完成，检查根目录和维护权限（不输出凭据）。')
        raise SystemExit(1) from None

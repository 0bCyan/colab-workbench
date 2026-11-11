"""Manage local artworks without touching remote jobs, sessions or credentials."""
import re
import shutil
import time

from .manager import ACTIVE


class Library:
    def __init__(self, manager):
        self.manager, self.store = manager, manager.store

    def manage(self, ids, action):
        ids = list(dict.fromkeys(ids))
        jobs = [self.manager.job(key) for key in ids]
        if any(job is None for job in jobs):
            raise ValueError("部分作品已不存在，请刷新后重试")
        if any(job["status"] in ACTIVE for job in jobs):
            raise ValueError("进行中的任务不能删除，请先取消任务")
        if action == "delete" and any(not job.get("deleted_at") for job in jobs):
            raise ValueError("请先将作品移入回收站")
        if action == "delete" and any(key in self.manager.inflight_jobs for key in ids):
            raise ValueError("任务正在收尾，请稍后再永久删除")
        if action in {"trash", "restore"}:
            stamp = time.time()
            for job in jobs:
                job.update(deleted_at=(job.get("deleted_at") or stamp) if action == "trash" else None,
                           updated_at=stamp)
            self.store.put_many("jobs", jobs)
            return {"ids": ids, "failed": []}

        # Validate the whole batch before any filesystem mutation. Never follow an output symlink.
        root = self.store.directory / "outputs"
        if root.is_symlink():
            raise ValueError("作品目录异常，已停止删除")
        folders = []
        for key in ids:
            folder = root / key
            if not re.fullmatch(r"[a-f0-9]{32}", key) or folder.is_symlink() or folder.resolve().parent != root.resolve():
                raise ValueError("作品路径异常，已停止删除")
            folders.append(folder)
        deleted, failed = [], []
        for key, folder in zip(ids, folders, strict=True):
            try:
                if folder.exists():
                    shutil.rmtree(folder)
                self.store.delete("jobs", key)
                deleted.append(key)
            except OSError:
                failed.append({"id": key, "reason": "部分本地文件未能删除，请检查权限后重试"})
        return {"ids": deleted, "failed": failed}

    def rename(self, key, name):
        job = self.manager.job(key)
        if job is None:
            raise ValueError("作品不存在")
        if job.get("deleted_at"):
            raise ValueError("请先恢复作品")
        name = name.strip()
        if not name:
            raise ValueError("名称不能为空")
        return self.manager.job(key, name=name)

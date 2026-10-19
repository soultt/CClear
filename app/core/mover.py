"""File / directory migration engine: copy -> verify -> replace with junction."""

from __future__ import annotations

import os
import shutil
import time
from typing import Callable, Optional

from . import journal, registry
from .util import (dir_size, fingerprint, free_space, human_size, is_reparse_point,
                   lexists, long_path, path_in, sanitize_name, unique_path)
from . import winutil

# Paths that must never be migrated wholesale.
FORBIDDEN = {
    "c:", "c:\\windows", "c:\\program files", "c:\\program files (x86)",
    "c:\\programdata", "c:\\users", "c:\\users\\public", "c:\\perflogs",
    "c:\\recovery", "c:\\$recycle.bin", "c:\\system volume information",
}
PROTECTED_SUBSTR = (
    "\\windows\\winsxs", "\\windows\\system32", "\\windows\\syswow64",
    "\\windows\\servicing", "\\windows\\assembly",
)


def validate(src: str, dst_root: str, *, allow_system: bool = False) -> tuple[bool, list[str], list[str]]:
    """Return (ok, errors, warnings) for a planned migration."""
    errors: list[str] = []
    warnings: list[str] = []
    src = os.path.abspath(src)
    dst_root = os.path.abspath(dst_root)

    if not os.path.exists(src):
        errors.append(f"源路径不存在：{src}")
        return False, errors, warnings
    if not os.path.isdir(dst_root):
        errors.append(f"目标磁盘目录不存在：{dst_root}")
        return False, errors, warnings

    if path_in(dst_root, src):
        errors.append("目标位置位于源目录内部，无法迁移。")
    if path_in(src, dst_root):
        errors.append("源目录位于目标位置内部，无法迁移。")

    src_drive = os.path.splitdrive(src)[0].upper()
    dst_drive = os.path.splitdrive(dst_root)[0].upper()
    if src_drive == dst_drive:
        warnings.append("源与目标在同一分区，迁移不会释放磁盘空间（仅整理位置）。")

    lowered = src.lower().rstrip("\\")
    if len(lowered) <= 2 and lowered.endswith(":"):
        errors.append("不能迁移整个分区根目录，请选择具体的子目录。")
    if lowered in FORBIDDEN:
        errors.append("该目录是系统关键目录，禁止整体迁移。")
    for sub in PROTECTED_SUBSTR:
        if sub in lowered:
            errors.append(f"该目录属于系统核心组件（{sub}），禁止迁移。")
    if lowered.startswith("c:\\windows") and not allow_system:
        warnings.append("目标位于 Windows 目录内，可能导致系统更新失败，请谨慎操作。")

    if is_reparse_point(src):
        errors.append("该路径已经是一个联接/符号链接，无需再次迁移。")

    if os.path.isfile(src):
        size = os.path.getsize(long_path(src))
    else:
        size, _ = dir_size(src, budget_seconds=20)
    free = free_space(dst_root)
    if free < size * 1.03:
        errors.append(
            f"目标磁盘剩余空间不足：需要 {human_size(int(size * 1.03))}，剩余 {human_size(free)}。"
        )
    return (not errors), errors, warnings


def destination_for(src: str, dst_root: str, group: str = "Migrated") -> str:
    """Where a source item will live on the target drive."""
    name = sanitize_name(os.path.basename(os.path.abspath(src.rstrip("\\"))) or "root")
    base = os.path.join(os.path.abspath(dst_root), group)
    drive = os.path.splitdrive(os.path.abspath(src))[0].replace(":", "")
    return os.path.join(base, f"{drive}-{name}" if drive else name)


def plan(
    src: str,
    dst_root: str,
    *,
    include_registry: bool = True,
    group: str = "Migrated",
) -> dict:
    """Build a full migration plan (no side effects)."""
    src = os.path.abspath(src)
    ok, errors, warnings = validate(src, dst_root)
    dst = destination_for(src, dst_root, group)
    summary = {"path": src, "size": 0, "size_human": "0 B", "count": 0}
    if os.path.exists(src):
        if os.path.isdir(src) and not is_reparse_point(src):
            s, c = dir_size(src, budget_seconds=25)
        else:
            s, c = os.path.getsize(long_path(src)), 1
        summary = {"path": src, "size": s, "size_human": human_size(s), "count": c}

    refs: list[dict] = []
    if include_registry and not errors:
        refs = registry.find_references(src, max_seconds=20)

    procs = winutil.processes_using(src)
    if procs:
        warnings.append("检测到以下进程可能正在使用该目录：" +
                        "、".join(f"{p['name']}({p['pid']})" for p in procs[:6]))
    if os.path.exists(dst):
        dst = unique_path(dst)
        warnings.append("目标已存在同名目录，将使用新名称以避免覆盖。")

    return {
        "src": src,
        "dst": dst,
        "dst_root": os.path.abspath(dst_root),
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": summary,
        "registry_refs": refs,
        "registry_count": len(refs),
        "is_junction": is_reparse_point(src),
        "link_target": os.readlink(long_path(src)) if is_reparse_point(src) else None,
        "processes": procs,
        "drives_ok": os.path.splitdrive(src)[0].upper() != os.path.splitdrive(dst)[0].upper(),
    }


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execute(
    src: str,
    dst: str,
    *,
    link: bool = True,
    update_registry: bool = True,
    verify_hash: bool = False,
    clean_source: bool = True,
    action: str = "move_dir",
    label: str = "",
    report: Optional[Callable[[float, str, dict], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Move ``src`` to ``dst``, verify, then leave a junction behind."""
    src = os.path.abspath(src)
    dst = os.path.abspath(dst)
    started = time.time()

    def emit(pct: float, msg: str, **detail) -> None:
        if report:
            report(pct, msg, detail)

    result = {
        "src": src, "dst": dst, "action": action, "label": label or os.path.basename(src),
        "link": False, "registry_backup": None, "registry_changed": 0,
        "registry_entries": [], "shortcuts": [], "size_bytes": 0, "files": 0,
        "verified": False, "reverted": False, "warnings": [],
    }

    if not lexists(src):
        raise RuntimeError(f"源路径不存在：{src}")
    if lexists(src) and not os.path.exists(src):
        raise RuntimeError(f"源路径是一个失效的链接，请先删除它：{src}")
    if lexists(dst):
        raise RuntimeError(f"目标已存在：{dst}")

    # 1) measure ---------------------------------------------------------
    emit(2, "正在计算目录大小…")
    if os.path.isdir(src) and not is_reparse_point(src):
        total, files = dir_size(src)
    else:
        total, files = os.path.getsize(long_path(src)), 1
    result["size_bytes"], result["files"] = total, files

    # 2) registry backup -------------------------------------------------
    entries: list[dict] = []
    if update_registry:
        emit(5, "正在检索注册表中的路径引用…")
        entries = registry.find_references(src, max_seconds=30, cancel=cancel)
        result["registry_entries"] = entries
        if entries:
            emit(7, f"发现 {len(entries)} 处注册表引用，正在备份…")
            try:
                result["registry_backup"] = registry.backup_keys(
                    {e["key_path"] for e in entries}, tag=sanitize_name(os.path.basename(src))
                )
            except Exception as exc:  # noqa: BLE001
                result["warnings"].append(f"注册表备份失败：{exc}")

    # 3) copy ------------------------------------------------------------
    if os.path.isdir(src):
        emit(10, f"正在复制到 {dst} …")

        def on_progress(cp: winutil.CopyProgress) -> None:
            emit(10 + cp.percent * 0.65, cp.message,
                 done_bytes=cp.done_bytes, total_bytes=cp.total_bytes, speed=cp.speed)

        ok, err = winutil.copy_tree(src, dst, report=on_progress, cancel=cancel,
                                    min_free_bytes=int(total * 1.02))
        if not ok:
            raise RuntimeError(err)
    else:
        emit(30, f"正在复制文件到 {dst} …")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(long_path(src), long_path(dst))

    # 4) verify ----------------------------------------------------------
    emit(78, "正在校验复制结果…")
    if os.path.isdir(dst):
        dst_total, dst_files = dir_size(dst)
        if dst_files < files:
            raise RuntimeError(f"校验失败：目标文件数 {dst_files} 少于源文件数 {files}")
        if total and abs(dst_total - total) > max(4096, total * 0.001):
            raise RuntimeError(f"校验失败：大小不一致（源 {human_size(total)} / 目标 {human_size(dst_total)}）")
        if verify_hash:
            emit(82, "正在比对文件指纹…")
            _, _, h1 = fingerprint(src)
            _, _, h2 = fingerprint(dst)
            if h1 != h2:
                raise RuntimeError("校验失败：内容指纹不一致")
        result["verified"] = True
    else:
        if os.path.getsize(long_path(dst)) != os.path.getsize(long_path(src)):
            raise RuntimeError("校验失败：文件大小不一致")
        result["verified"] = True

    # 5) remove source + create junction ---------------------------------
    if clean_source:
        emit(86, "正在删除源目录…")
        was_dir = os.path.isdir(src) and not is_reparse_point(src)
        ok, err = winutil.delete_tree(src) if was_dir else _remove_file(src)
        if not ok:
            result["warnings"].append(f"源目录删除不完整：{err}")
        if link and not os.path.exists(src):
            emit(90, "正在创建链接…" if not was_dir else "正在创建目录联接…")
            if was_dir:
                ok, err = winutil.create_junction(src, dst)
            else:
                ok, err = winutil.create_symlink(src, dst)
            if ok:
                result["link"] = True
            else:
                result["warnings"].append(
                    f"链接创建失败，应用或快捷方式可能需要重新指向：{err}"
                )

    # 6) registry + shortcuts --------------------------------------------
    if update_registry and entries:
        emit(94, f"正在同步 {len(entries)} 处注册表引用…")
        changed, errs = registry.rewrite_references(entries, src, dst)
        result["registry_changed"] = changed
        if errs:
            result["warnings"].extend(errs[:10])

    # 7) shortcuts --------------------------------------------------------
    emit(97, "正在更新快捷方式…")
    try:
        result["shortcuts"] = winutil.update_shortcuts(src, dst)
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(f"快捷方式更新失败：{exc}")

    result.update({"seconds": round(time.time() - started, 1), "status": "done"})
    emit(100, "迁移完成")
    registry.clear_cache()
    journal.add(result)
    return result


def _remove_file(path: str) -> tuple[bool, str]:
    try:
        os.remove(long_path(path))
        return True, ""
    except OSError as exc:
        return False, str(exc)


# ---------------------------------------------------------------------------
# Revert
# ---------------------------------------------------------------------------


def revert(record: dict, *, report: Optional[Callable[[float, str, dict], None]] = None,
           cancel: Optional[Callable[[], bool]] = None) -> dict:
    """Undo a migration: restore registry, move data back, drop the junction."""
    src = record.get("src")
    dst = record.get("dst")
    if not src or not dst:
        raise RuntimeError("迁移记录不完整，无法还原")
    if record.get("reverted"):
        raise RuntimeError("该记录已经还原过了")
    if not os.path.exists(dst):
        raise RuntimeError(f"迁移后的数据已不存在：{dst}")

    def emit(pct: float, msg: str, **detail) -> None:
        if report:
            report(pct, msg, detail)

    notes: list[str] = []

    # registry first, so the restored paths point at the original location
    backup = record.get("registry_backup")
    if backup and os.path.isdir(backup):
        emit(4, "正在还原注册表…")
        ok, total, errors = registry.restore_backup(backup)
        notes.append(f"注册表备份已还原 {ok}/{total}")
        if errors:
            notes.extend(errors[:5])

    # drop junction
    if os.path.exists(src):
        if is_reparse_point(src):
            emit(8, "正在移除目录联接…")
            ok, err = winutil.remove_reparse_point(src)
            if not ok:
                raise RuntimeError(err)
        else:
            raise RuntimeError(f"原路径已存在真实数据，无法自动还原：{src}")

    emit(10, "正在把数据移回原位置…")
    os.makedirs(os.path.dirname(src), exist_ok=True)

    def on_progress(cp: winutil.CopyProgress) -> None:
        emit(10 + cp.percent * 0.8, cp.message,
             done_bytes=cp.done_bytes, total_bytes=cp.total_bytes, speed=cp.speed)

    if os.path.isdir(dst):
        ok, err = winutil.copy_tree(dst, src, report=on_progress, cancel=cancel)
    else:
        os.makedirs(os.path.dirname(src), exist_ok=True)
        shutil.copy2(long_path(dst), long_path(src))
        ok, err = True, ""
    if not ok:
        raise RuntimeError(f"还原复制失败：{err}")

    emit(92, "正在校验并清理目标位置…")
    if os.path.isdir(dst):
        _t, orig_files = dir_size(src)
        if orig_files == 0 and record.get("files"):
            raise RuntimeError("还原校验失败：目标位置为空")
        winutil.delete_tree(dst)
    else:
        _remove_file(dst)

    if record.get("shortcuts"):
        emit(96, "正在恢复快捷方式…")
        winutil.update_shortcuts(dst, src)

    journal.update(record["id"], reverted=True, reverted_at=time.time(), revert_notes=notes)
    registry.clear_cache()
    emit(100, "已还原到原位置")
    return {"src": src, "dst": dst, "notes": notes}


def rollback_last() -> Optional[dict]:
    for rec in journal.all_records():
        if rec.get("action") in ("move_file", "move_dir", "move_app") and not rec.get("reverted"):
            return revert(rec)
    return None

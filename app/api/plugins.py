"""
API Router: Plugins management.

Endpoints pour lister, installer, désinstaller les plugins + gérer les
dépôts (officiel + custom).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse, Response as FastAPIResponse
from pydantic import BaseModel, Field

from ..core.plugin_base import PluginError
from ..logger import logger

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

_plugin_manager = None
_db = None

DEFAULT_ICON_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" fill="none">'
    b'<rect width="64" height="64" rx="12" fill="#1f2937"/>'
    b'<path d="M20 22h8v-4a4 4 0 118 0v4h8a4 4 0 014 4v8h-4a4 4 0 100 8h4v8a4 4 0 01-4 4h-8v-4a4 4 0 10-8 0v4h-8a4 4 0 01-4-4v-8h4a4 4 0 100-8h-4v-8a4 4 0 014-4z" fill="#6366f1"/>'
    b'</svg>'
)


def init(plugin_manager, db):
    global _plugin_manager, _db
    _plugin_manager = plugin_manager
    _db = db


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PluginInfo(BaseModel):
    id: str
    name: str
    version: str
    author: Optional[str] = None
    description: Optional[str] = ""
    source_type: str
    source_repo: Optional[str] = None
    status: str
    enabled: bool
    installed: bool
    official: bool = False
    verified: bool = False
    capabilities: List[str] = []
    homepage: Optional[str] = None
    last_error: Optional[str] = None
    builtin: bool = False
    icon_url: Optional[str] = None


class RepoInfo(BaseModel):
    id: str
    name: str
    index_url: str
    verified: bool
    builtin: bool
    last_sync: Optional[int] = None


class InstallBody(BaseModel):
    plugin_id: str = Field(min_length=1, max_length=64)
    repo_id: Optional[str] = None
    acknowledge_risk: bool = False


class RepoAddBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    index_url: str = Field(min_length=8)


def _require_manager():
    if _plugin_manager is None:
        raise HTTPException(status_code=503, detail="Plugin manager non initialisé")
    return _plugin_manager


def _loaded_to_info(loaded) -> PluginInfo:
    return PluginInfo(
        id=loaded.manifest.id,
        name=loaded.manifest.name,
        version=loaded.manifest.version,
        author=loaded.manifest.author,
        description=loaded.manifest.description,
        source_type=loaded.manifest.source_type,
        source_repo=loaded.source_repo,
        status=loaded.status,
        enabled=loaded.status == "loaded",
        installed=True,
        official=bool(loaded.manifest.official),
        verified=True,
        capabilities=list(loaded.manifest.capabilities),
        homepage=loaded.manifest.homepage,
        builtin=loaded.builtin,
        icon_url=f"/api/plugins/icon/{loaded.manifest.id}",
    )


# ---------------------------------------------------------------------------
# Installed
# ---------------------------------------------------------------------------


@router.get("/installed", response_model=List[PluginInfo])
async def list_installed():
    pm = _require_manager()
    result: List[PluginInfo] = []

    # 1) Plugins chargés dans le registry
    loaded_ids = set()
    for loaded in pm.registry.list_plugins():
        result.append(_loaded_to_info(loaded))
        loaded_ids.add(loaded.manifest.id)

    # 2) Records DB qui ne sont pas chargés (installed-mais-en-erreur ou pending_restart)
    records = await _db.plugin_list_records()
    for rec in records:
        if rec["id"] in loaded_ids:
            continue
        result.append(
            PluginInfo(
                id=rec["id"],
                name=rec["id"],
                version=rec["version"],
                source_type=rec["source_type"],
                source_repo=rec.get("source_repo"),
                status=rec.get("status") or "pending_restart",
                enabled=bool(rec.get("enabled")),
                installed=bool(rec.get("installed")),
                last_error=rec.get("last_error"),
                icon_url=f"/api/plugins/icon/{rec['id']}",
            )
        )

    return result


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@router.get("/catalog", response_model=List[PluginInfo])
async def list_catalog(repo_id: Optional[str] = Query(None)):
    pm = _require_manager()
    try:
        entries = await pm.fetch_catalog(repo_id=repo_id)
    except PluginError as e:
        raise HTTPException(status_code=400, detail=str(e))

    records = {r["id"]: r for r in await _db.plugin_list_records()}
    results: List[PluginInfo] = []
    for entry in entries:
        pid = entry.get("id")
        if not pid:
            continue
        record = records.get(pid)
        is_installed = record is not None and bool(record.get("installed"))
        results.append(
            PluginInfo(
                id=pid,
                name=entry.get("name") or pid,
                version=entry.get("version") or "?",
                author=entry.get("author"),
                description=entry.get("description", ""),
                source_type=entry.get("source_type") or pid,
                source_repo=entry.get("_repo_id"),
                status=(record.get("status") if record else "available"),
                enabled=bool(record.get("enabled")) if record else False,
                installed=is_installed,
                official=bool(entry.get("official", False)),
                verified=bool(entry.get("_repo_verified", False)),
                capabilities=list(entry.get("capabilities", [])),
                homepage=entry.get("homepage"),
                icon_url=entry.get("icon_url") or f"/api/plugins/icon/{pid}",
            )
        )
    return results


# ---------------------------------------------------------------------------
# Install / Uninstall / Enable / Disable
# ---------------------------------------------------------------------------


@router.post("/install")
async def install_plugin(body: InstallBody):
    pm = _require_manager()
    try:
        manifest = await pm.install(
            plugin_id=body.plugin_id,
            repo_id=body.repo_id,
            acknowledge_risk=body.acknowledge_risk,
        )
    except PluginError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Install plugin failed", plugin_id=body.plugin_id, error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "success": True,
        "restart_required": True,
        "message": f"Plugin '{manifest.id}' v{manifest.version} installé. Redémarrez pour l'activer.",
    }


@router.post("/uninstall/{plugin_id}")
async def uninstall_plugin(plugin_id: str):
    pm = _require_manager()
    # Refuser si une session FFmpeg active utilise ce source_type
    try:
        loaded = pm.registry.get_by_id(plugin_id)
        if loaded and not loaded.builtin:
            # Import tardif pour éviter le cycle
            from .. import main as app_main  # type: ignore
            active = app_main.manager.list_status()
            for s in active:
                if s.get("running") and s.get("source_type") == loaded.manifest.source_type:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Session active utilisant {loaded.manifest.source_type}",
                    )
    except HTTPException:
        raise
    except Exception:
        pass

    try:
        await pm.uninstall(plugin_id)
        # Retire aussi du registry si chargé
        pm.registry.unregister(plugin_id)
        pm.loaded.pop(plugin_id, None)
    except PluginError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "restart_required": True}


@router.post("/enable/{plugin_id}")
async def enable_plugin(plugin_id: str):
    pm = _require_manager()
    try:
        await pm.set_enabled(plugin_id, True)
    except PluginError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "restart_required": True}


@router.post("/disable/{plugin_id}")
async def disable_plugin(plugin_id: str):
    pm = _require_manager()
    try:
        await pm.set_enabled(plugin_id, False)
    except PluginError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "restart_required": True}


# ---------------------------------------------------------------------------
# Repos
# ---------------------------------------------------------------------------


@router.get("/repos", response_model=List[RepoInfo])
async def list_repos():
    pm = _require_manager()
    repos = await pm.list_repos()
    return [
        RepoInfo(
            id=r.id,
            name=r.name,
            index_url=r.index_url,
            verified=r.verified,
            builtin=r.builtin,
            last_sync=r.last_sync,
        )
        for r in repos
    ]


@router.post("/repos", response_model=RepoInfo)
async def add_repo(body: RepoAddBody):
    pm = _require_manager()
    try:
        repo = await pm.add_repo(name=body.name, index_url=body.index_url)
    except PluginError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Add repo failed", error=str(e), exc_info=True)
        raise HTTPException(status_code=400, detail=f"Impossible d'ajouter le dépôt: {e}")
    return RepoInfo(
        id=repo.id,
        name=repo.name,
        index_url=repo.index_url,
        verified=repo.verified,
        builtin=repo.builtin,
        last_sync=repo.last_sync,
    )


@router.delete("/repos/{repo_id}")
async def remove_repo(repo_id: str):
    pm = _require_manager()
    try:
        await pm.remove_repo(repo_id)
    except PluginError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True}


@router.post("/repos/{repo_id}/refresh")
async def refresh_repo(repo_id: str):
    pm = _require_manager()
    try:
        count = await pm.refresh_repo(repo_id)
    except PluginError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Refresh échoué: {e}")
    return {"success": True, "plugin_count": count}


# ---------------------------------------------------------------------------
# Icon
# ---------------------------------------------------------------------------


@router.get("/icon/{plugin_id}")
async def get_plugin_icon(plugin_id: str):
    pm = _require_manager()
    # 1) Plugin installé: lire icon.png du dossier plugin
    plugin_dir = pm.plugins_root / plugin_id
    icon_path = plugin_dir / "icon.png"
    if icon_path.is_file():
        return FileResponse(
            str(icon_path),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    # 2) Fallback SVG générique
    return FastAPIResponse(
        content=DEFAULT_ICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


@router.post("/restart")
async def trigger_restart():
    """Touche main.py pour déclencher un reload uvicorn."""
    try:
        # Import tardif: évite cycle de chargement
        from .. import main as app_main  # type: ignore
        asyncio.create_task(app_main.restart_application())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Restart failed: {e}")
    return {"success": True, "message": "Redémarrage en cours..."}

"""
Adapter qui expose Chaturbate comme un plugin builtin dans le registry.

Wrappe les fonctions existantes (resolvers.chaturbate, tasks.monitor) pour
que `/api/start`, `/api/model/*/status` et `monitor_models_task` passent par
le même code path que les plugins tiers.

Chaturbate reste intégré au core (pas téléchargeable) mais respecte l'interface
SourcePlugin pour valider le contrat.
"""
from __future__ import annotations
from typing import Optional

import aiohttp

from .plugin_base import (
    ModelStatus,
    PluginContext,
    PluginManifest,
    PluginResolveError,
    PluginStatusError,
    ResolveResult,
)


class ChaturbateBuiltinPlugin:
    """Plugin builtin pour la source Chaturbate."""

    def __init__(self):
        self.manifest = PluginManifest(
            id="chaturbate",
            name="Chaturbate",
            version="builtin",
            author="raccommode",
            description="Source Chaturbate (intégrée).",
            api_version=1,
            source_type="chaturbate",
            capabilities=["resolve", "check_status"],
            official=True,
        )
        self._ctx: Optional[PluginContext] = None

    def init(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    def shutdown(self) -> None:
        pass

    def validate_target(self, target: str) -> bool:
        if not target:
            return False
        t = target.strip().lower()
        # Username Chaturbate: [a-z0-9_]+
        return bool(t) and all(c.isalnum() or c == "_" for c in t)

    async def resolve(
        self, target: str, max_height: Optional[int] = None
    ) -> ResolveResult:
        # Import tardif pour éviter les cycles d'import au module load
        from ..resolvers.chaturbate import resolve_m3u8_async, resolve_m3u8

        try:
            url = await resolve_m3u8_async(target, max_height=max_height)
        except Exception:
            url = None

        if not url:
            try:
                url = resolve_m3u8(target)
            except Exception as e:
                raise PluginResolveError(
                    f"Échec résolution Chaturbate pour '{target}': {e}"
                )

        if not url:
            raise PluginResolveError(f"Aucun M3U8 trouvé pour '{target}'")
        return ResolveResult(m3u8_url=url)

    async def check_status(self, username: str) -> ModelStatus:
        from ..tasks.monitor import check_model_status
        from ..services.chaturbate_auth import ChaturbateAuthService  # noqa: F401

        # On ouvre une session dédiée pour respecter l'interface plugin
        # (le monitor_models_task pourra continuer à mutualiser sa session).
        csrftoken = None
        try:
            if self._ctx is not None:
                csrftoken = await self._ctx.db_get_setting("csrftoken")
        except Exception:
            csrftoken = None

        async with aiohttp.ClientSession() as session:
            try:
                data = await check_model_status(session, username, csrftoken)
            except Exception as e:
                raise PluginStatusError(
                    f"Échec check_status Chaturbate pour '{username}': {e}"
                )

        return ModelStatus(
            is_online=bool(data.get("is_online", False)),
            viewers=int(data.get("viewers", 0) or 0),
            hls_source=data.get("hls_source"),
        )


plugin = ChaturbateBuiltinPlugin()

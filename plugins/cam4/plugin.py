"""
Plugin CAM4 - source optionnelle (auto_install: false).

Résout un username CAM4 vers son playlist HLS via l'API publique
getBroadcasting. Aucun login requis pour les profils publics.
"""
from __future__ import annotations
import re
from typing import Any, Dict, Optional

import aiohttp

from app.core.plugin_base import (
    ModelStatus,
    PluginContext,
    PluginManifest,
    PluginResolveError,
    PluginStatusError,
    ResolveResult,
)


PROFILE_PAGE_URL = "https://www.cam4.com/{username}"
BROADCAST_API_URL = "https://www.cam4.com/rest/v1.0/profile/{username}/streamInfo"

# Regex de secours pour extraire un .m3u8 du HTML si l'API change.
_M3U8_RE = re.compile(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*", re.IGNORECASE)
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{2,32}$")


class CAM4Plugin:
    """Plugin officiel pour la source CAM4."""

    def __init__(self):
        self.manifest = PluginManifest(
            id="cam4",
            name="CAM4",
            version="1.0.0",
            author="raccommode",
            description="Source CAM4 — installation optionnelle depuis le catalogue.",
            api_version=1,
            source_type="cam4",
            capabilities=["resolve", "check_status"],
            official=True,
            auto_install=False,
            homepage="https://www.cam4.com",
        )
        self._ctx: Optional[PluginContext] = None

    def init(self, ctx: PluginContext) -> None:
        self._ctx = ctx

    def shutdown(self) -> None:
        pass

    def validate_target(self, target: str) -> bool:
        if not target:
            return False
        return bool(_USERNAME_RE.match(target.strip()))

    async def resolve(
        self, target: str, max_height: Optional[int] = None
    ) -> ResolveResult:
        username = target.strip().lower()
        if not self.validate_target(username):
            raise PluginResolveError(f"Username CAM4 invalide: '{target}'")

        info = await self._fetch_stream_info(username)
        hls = info.get("cdnURL") or info.get("hlsPlaylistUrl") or info.get("edgeURL")
        if not hls:
            # Fallback: scan the profile HTML for a .m3u8
            hls = await self._scrape_hls(username)

        if not hls:
            raise PluginResolveError(f"Aucun M3U8 trouvé pour CAM4/{username}")

        return ResolveResult(m3u8_url=hls)

    async def check_status(self, username: str) -> ModelStatus:
        if not self.validate_target(username):
            raise PluginStatusError(f"Username CAM4 invalide: '{username}'")
        try:
            info = await self._fetch_stream_info(username.strip().lower())
        except Exception as e:
            raise PluginStatusError(f"Échec check_status CAM4 '{username}': {e}")

        is_online = bool(info.get("isLive") or info.get("isCamming") or info.get("online"))
        viewers = int(info.get("viewerCount") or info.get("viewers") or 0)
        hls = info.get("cdnURL") or info.get("hlsPlaylistUrl") or info.get("edgeURL")

        return ModelStatus(
            is_online=is_online,
            viewers=viewers,
            hls_source=hls,
        )

    async def _fetch_stream_info(self, username: str) -> Dict[str, Any]:
        """Try the CAM4 streamInfo REST endpoint; return {} on failure."""
        ua = (
            self._ctx.http_user_agent
            if self._ctx is not None
            else "Mozilla/5.0 P-StreamRec/PluginSDK-1"
        )
        timeout = aiohttp.ClientTimeout(total=15)
        url = BROADCAST_API_URL.format(username=username)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"User-Agent": ua}) as resp:
                    if resp.status != 200:
                        return {}
                    data = await resp.json(content_type=None)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    async def _scrape_hls(self, username: str) -> Optional[str]:
        ua = (
            self._ctx.http_user_agent
            if self._ctx is not None
            else "Mozilla/5.0 P-StreamRec/PluginSDK-1"
        )
        timeout = aiohttp.ClientTimeout(total=15)
        url = PROFILE_PAGE_URL.format(username=username)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"User-Agent": ua}) as resp:
                    if resp.status != 200:
                        return None
                    html = await resp.text()
            m = _M3U8_RE.search(html)
            return m.group(0) if m else None
        except Exception:
            return None


plugin = CAM4Plugin()

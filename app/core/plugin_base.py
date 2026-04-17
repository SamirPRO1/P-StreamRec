"""
SDK de contrat pour les plugins de sources de streaming.

Tout plugin doit implémenter la classe SourcePlugin (via duck-typing sur le
Protocol) et exposer une instance via l'attribut `plugin` de son module racine.

Les plugins n'importent QUE ce module depuis l'app. Tout autre import interne
(app.main, app.core.database, services Chaturbate...) est interdit et rend le
plugin fragile aux breaking changes.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

PLUGIN_API_VERSION = 1


class PluginError(Exception):
    """Erreur générique plugin."""


class PluginResolveError(PluginError):
    """Levée quand un plugin ne peut pas résoudre un target vers un M3U8."""


class PluginStatusError(PluginError):
    """Levée quand check_status échoue."""


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str
    author: str
    description: str
    api_version: int
    source_type: str
    capabilities: List[str] = field(default_factory=list)
    homepage: Optional[str] = None
    min_app_version: Optional[str] = None
    official: bool = False
    # When true (default), the plugin is auto-installed on first boot from its
    # bundled sources. Set false for optional plugins that users must opt in to
    # via the Plugin Catalog (Settings → Plugins → Advanced).
    auto_install: bool = True
    icon_path: Optional[Path] = None


@dataclass
class ResolveResult:
    m3u8_url: str
    quality_label: Optional[str] = None


@dataclass
class ModelStatus:
    is_online: bool
    viewers: int = 0
    hls_source: Optional[str] = None
    thumbnail_url: Optional[str] = None
    display_name: Optional[str] = None


@dataclass
class PluginContext:
    """
    Contexte injecté dans chaque plugin via init(ctx).

    - data_dir: dossier scopé par plugin pour stocker des caches/state
    - db_get_setting / db_set_setting: accès DB scopé par préfixe plugin:<id>:
    - http_user_agent: UA commun à utiliser pour les requêtes HTTP
    - logger: logger duck-typé (info/debug/warning/error/success)
    """
    data_dir: Path
    db_get_setting: Callable[[str], Awaitable[Optional[str]]]
    db_set_setting: Callable[[str, str], Awaitable[None]]
    http_user_agent: str
    logger: Any


class SourcePlugin(Protocol):
    """
    Contrat qu'un plugin DOIT implémenter.

    Le PluginManager valide la présence des méthodes + de l'attribut manifest
    via duck-typing au chargement.
    """
    manifest: PluginManifest

    def init(self, ctx: PluginContext) -> None: ...
    def shutdown(self) -> None: ...
    def validate_target(self, target: str) -> bool: ...
    async def resolve(
        self, target: str, max_height: Optional[int] = None
    ) -> ResolveResult: ...
    async def check_status(self, username: str) -> ModelStatus: ...


REQUIRED_METHODS = ("init", "shutdown", "validate_target", "resolve", "check_status")


def validate_plugin_instance(instance: Any, expected_id: str) -> None:
    """
    Vérifie qu'une instance respecte le contrat. Lève PluginError sinon.

    Appelé par le PluginManager au chargement du plugin.
    """
    if not hasattr(instance, "manifest") or instance.manifest is None:
        raise PluginError(f"Plugin '{expected_id}': attribut 'manifest' manquant")
    for method in REQUIRED_METHODS:
        if not callable(getattr(instance, method, None)):
            raise PluginError(f"Plugin '{expected_id}': méthode '{method}' manquante")
    if instance.manifest.id != expected_id:
        raise PluginError(
            f"Plugin: ID mismatch (dossier='{expected_id}', "
            f"manifest.id='{instance.manifest.id}')"
        )


def manifest_from_dict(data: Dict[str, Any]) -> PluginManifest:
    """
    Parse un dict (issu de manifest.json) vers PluginManifest.
    Lève PluginError si un champ requis manque.
    """
    required = ("id", "name", "version", "author", "description", "api_version", "source_type")
    missing = [k for k in required if k not in data]
    if missing:
        raise PluginError(f"Manifest: champs manquants: {', '.join(missing)}")
    return PluginManifest(
        id=data["id"],
        name=data["name"],
        version=data["version"],
        author=data["author"],
        description=data["description"],
        api_version=int(data["api_version"]),
        source_type=data["source_type"],
        capabilities=list(data.get("capabilities", [])),
        homepage=data.get("homepage"),
        min_app_version=data.get("min_app_version"),
        official=bool(data.get("official", False)),
        auto_install=bool(data.get("auto_install", True)),
    )

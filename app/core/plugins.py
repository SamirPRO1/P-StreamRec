"""
PluginManager - découverte, chargement, install/uninstall des plugins.

Design:
- Les plugins installés vivent dans <plugins_root>/<plugin_id>/
- Le parent du plugins_root est ajouté à sys.path pour rendre importable
  `plugins.<id>` (seulement si plugins_root s'appelle "plugins").
- Pas de hot-reload: install/uninstall/toggle marquent pending_restart.
- Registry indexé par source_type, collision détectée au register.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from ..logger import logger
from .plugin_base import (
    PLUGIN_API_VERSION,
    ModelStatus,
    PluginContext,
    PluginError,
    PluginManifest,
    ResolveResult,
    manifest_from_dict,
    validate_plugin_instance,
)


OFFICIAL_REPO_ID = "official"
OFFICIAL_REPO_NAME = "Official"
# Catalogue officiel hébergé dans le repo principal sous plugins/. Même nom
# que le dossier runtime (/data/plugins/) : pas d'ambiguïté, les contextes
# (source tree vs runtime) sont distincts.
OFFICIAL_REPO_INDEX_URL = (
    "https://raw.githubusercontent.com/raccommode/P-StreamRec/main/plugins/index.json"
)
SETTINGS_KEY_REPOS = "plugin_repos"
CATALOG_CACHE_TTL_SECONDS = 300


@dataclass
class LoadedPlugin:
    manifest: PluginManifest
    instance: Any
    module_path: str
    source_repo: Optional[str] = None
    status: str = "loaded"  # loaded | error | disabled
    error: Optional[str] = None


@dataclass
class RepoConfig:
    id: str
    name: str
    index_url: str
    verified: bool = False
    builtin: bool = False
    last_sync: Optional[int] = None


class SourceRegistry:
    """Map source_type -> LoadedPlugin. Collision détectée au register."""

    def __init__(self):
        self._by_source: Dict[str, LoadedPlugin] = {}
        self._by_id: Dict[str, LoadedPlugin] = {}

    def register(self, plugin: LoadedPlugin) -> None:
        st = plugin.manifest.source_type
        pid = plugin.manifest.id
        if st in self._by_source:
            raise PluginError(
                f"source_type '{st}' déjà enregistré par '{self._by_source[st].manifest.id}'"
            )
        if pid in self._by_id:
            raise PluginError(f"plugin id '{pid}' déjà enregistré")
        self._by_source[st] = plugin
        self._by_id[pid] = plugin

    def unregister(self, plugin_id: str) -> None:
        plugin = self._by_id.pop(plugin_id, None)
        if plugin is not None:
            self._by_source.pop(plugin.manifest.source_type, None)

    def get(self, source_type: str) -> Optional[LoadedPlugin]:
        return self._by_source.get(source_type)

    def get_by_id(self, plugin_id: str) -> Optional[LoadedPlugin]:
        return self._by_id.get(plugin_id)

    def list_source_types(self) -> List[str]:
        return list(self._by_source.keys())

    def list_plugins(self) -> List[LoadedPlugin]:
        return list(self._by_id.values())


class PluginManager:
    """Orchestre le cycle de vie des plugins."""

    MAX_ARCHIVE_BYTES = 50 * 1024 * 1024  # 50 MB safety cap

    def __init__(
        self,
        db,
        plugins_root: Path,
        data_root: Path,
        bundled_plugins_dir: Optional[Path] = None,
    ):
        self.db = db
        self.plugins_root = Path(plugins_root)
        self.data_root = Path(data_root)
        self.bundled_plugins_dir = (
            Path(bundled_plugins_dir) if bundled_plugins_dir else None
        )
        self.registry = SourceRegistry()
        self.loaded: Dict[str, LoadedPlugin] = {}
        self._catalog_cache: Dict[str, Tuple[float, dict]] = {}  # repo_id -> (ts, index)

    # ------------------------------------------------------------------
    # Bootstrap / lifecycle
    # ------------------------------------------------------------------

    async def ensure_bootstrap(self) -> None:
        """
        Prépare le filesystem et le sys.path pour importer les plugins.

        Crée /data/plugins/, ajoute /data au sys.path pour que
        `import plugins.<id>` fonctionne, copie les plugins bundle depuis
        les sources du repo au premier démarrage, et s'assure que le repo
        officiel est présent dans les settings.
        """
        self.plugins_root.mkdir(parents=True, exist_ok=True)
        init_file = self.plugins_root / "__init__.py"
        if not init_file.exists():
            init_file.write_text("# plugins namespace (géré par P-StreamRec)\n")

        parent = str(self.plugins_root.parent.resolve())
        if parent not in sys.path:
            sys.path.insert(0, parent)
            logger.debug("Plugin path ajouté à sys.path", path=parent)

        await self._ensure_official_repo()
        await self.ensure_bundled_plugins()

    async def ensure_bundled_plugins(self) -> None:
        """
        Copie les plugins bundle depuis bundled_plugins_dir (sources repo)
        vers plugins_root (runtime) lors du tout premier démarrage.

        Pour chaque sous-dossier <id> contenant un manifest.json dans
        bundled_plugins_dir : si aucun record DB n'existe pour <id>, copie
        les sources vers /data/plugins/<id> et crée un record installed=1,
        enabled=1, source_repo=OFFICIAL_REPO_ID. Aucun accès réseau requis.
        """
        if self.bundled_plugins_dir is None or not self.bundled_plugins_dir.is_dir():
            return

        for entry in sorted(self.bundled_plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            manifest_path = entry / "manifest.json"
            if not manifest_path.is_file():
                continue
            plugin_id = entry.name

            existing = await self.db.plugin_get_record(plugin_id)
            if existing is not None:
                continue  # Déjà connu (installé ou désinstallé par l'utilisateur)

            try:
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = manifest_from_dict(json.load(f))
            except Exception as e:
                logger.warning(
                    "Manifest bundle invalide, ignoré",
                    plugin_id=plugin_id,
                    error=str(e),
                )
                continue

            if not manifest.auto_install:
                # Optional plugin: user must install it manually via the catalog.
                continue

            target = self.plugins_root / plugin_id
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(entry, target)

            await self.db.plugin_upsert_record(
                plugin_id=plugin_id,
                version=manifest.version,
                source_type=manifest.source_type,
                source_repo=OFFICIAL_REPO_ID,
                enabled=True,
                installed=True,
                status="pending_restart",
                manifest_json=None,
            )
            logger.info(
                "Plugin bundle auto-installé",
                plugin_id=plugin_id,
                version=manifest.version,
            )

    async def load_all(self) -> None:
        """Charge tous les plugins installés+enabled depuis la DB."""
        records = await self.db.plugin_list_records()
        loaded_count = 0
        for rec in records:
            if not rec.get("installed"):
                continue
            if not rec.get("enabled"):
                await self.db.plugin_set_status(rec["id"], "disabled")
                continue
            plugin_id = rec["id"]
            try:
                self._load_one(plugin_id, source_repo=rec.get("source_repo"))
                await self.db.plugin_set_status(plugin_id, "loaded", error=None)
                loaded_count += 1
            except Exception as e:
                logger.error(
                    "Échec du chargement plugin",
                    plugin_id=plugin_id,
                    error=str(e),
                    exc_info=True,
                )
                await self.db.plugin_set_status(plugin_id, "error", error=str(e))
        logger.info("Plugins chargés", count=loaded_count)

    def _load_one(self, plugin_id: str, source_repo: Optional[str] = None) -> LoadedPlugin:
        plugin_dir = self.plugins_root / plugin_id
        if not plugin_dir.is_dir():
            raise PluginError(f"Dossier plugin introuvable: {plugin_dir}")

        manifest_path = plugin_dir / "manifest.json"
        if not manifest_path.is_file():
            raise PluginError(f"manifest.json introuvable dans {plugin_dir}")

        with open(manifest_path, encoding="utf-8") as f:
            manifest = manifest_from_dict(json.load(f))

        if manifest.id != plugin_id:
            raise PluginError(
                f"Manifest id='{manifest.id}' ne correspond pas au dossier '{plugin_id}'"
            )
        if manifest.api_version != PLUGIN_API_VERSION:
            raise PluginError(
                f"API version incompatible: plugin={manifest.api_version}, "
                f"core={PLUGIN_API_VERSION}"
            )

        module_name = f"{self.plugins_root.name}.{plugin_id}"
        if module_name in sys.modules:
            # Force reload pour récupérer d'éventuelles modifs (dev ou après reinstall)
            del sys.modules[module_name]
            # Nettoie aussi les sous-modules
            for key in list(sys.modules.keys()):
                if key.startswith(module_name + "."):
                    del sys.modules[key]

        module = importlib.import_module(module_name)
        if not hasattr(module, "plugin"):
            raise PluginError(f"Module '{module_name}' ne définit pas d'attribut 'plugin'")
        instance = module.plugin
        validate_plugin_instance(instance, plugin_id)

        # Attacher le chemin de l'icône si présente
        icon_path = plugin_dir / "icon.png"
        if icon_path.exists():
            manifest.icon_path = icon_path

        ctx = self._make_context(manifest)
        instance.init(ctx)

        loaded = LoadedPlugin(
            manifest=manifest,
            instance=instance,
            module_path=module_name,
            source_repo=source_repo,
            status="loaded",
        )
        self.registry.register(loaded)
        self.loaded[plugin_id] = loaded
        logger.info(
            "Plugin chargé",
            id=plugin_id,
            version=manifest.version,
            source_type=manifest.source_type,
        )
        return loaded

    def shutdown_all(self) -> None:
        for loaded in list(self.loaded.values()):
            try:
                loaded.instance.shutdown()
            except Exception as e:
                logger.warning(
                    "Erreur shutdown plugin", id=loaded.manifest.id, error=str(e)
                )

    def _make_context(self, manifest: PluginManifest) -> PluginContext:
        data_dir = self.data_root / "plugins_data" / manifest.id
        data_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"plugin:{manifest.id}:"

        async def _get(key: str) -> Optional[str]:
            return await self.db.get_setting(prefix + key)

        async def _set(key: str, value: str) -> None:
            await self.db.set_setting(prefix + key, value)

        return PluginContext(
            data_dir=data_dir,
            db_get_setting=_get,
            db_set_setting=_set,
            http_user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 "
                "P-StreamRec/PluginSDK-1"
            ),
            logger=logger,
        )

    # ------------------------------------------------------------------
    # Repos configuration
    # ------------------------------------------------------------------

    async def _ensure_official_repo(self) -> None:
        repos = await self.list_repos()
        existing = next((r for r in repos if r.id == OFFICIAL_REPO_ID), None)
        if existing is None:
            repos.insert(
                0,
                RepoConfig(
                    id=OFFICIAL_REPO_ID,
                    name=OFFICIAL_REPO_NAME,
                    index_url=OFFICIAL_REPO_INDEX_URL,
                    verified=True,
                    builtin=True,
                ),
            )
            await self._save_repos(repos)
        elif existing.index_url != OFFICIAL_REPO_INDEX_URL:
            # Migration: l'URL canonique a changé (ex: plugins-registry → plugins).
            # Forcer la mise à jour pour que les installs pointent au bon endroit.
            existing.index_url = OFFICIAL_REPO_INDEX_URL
            existing.verified = True
            existing.builtin = True
            self._catalog_cache.pop(OFFICIAL_REPO_ID, None)
            await self._save_repos(repos)
            logger.info(
                "URL du dépôt officiel mise à jour",
                new_url=OFFICIAL_REPO_INDEX_URL,
            )

    async def list_repos(self) -> List[RepoConfig]:
        raw = await self.db.get_setting(SETTINGS_KEY_REPOS)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("plugin_repos JSON invalide en DB, reset")
            return []
        return [
            RepoConfig(
                id=r["id"],
                name=r.get("name", r["id"]),
                index_url=r["index_url"],
                verified=bool(r.get("verified", False)),
                builtin=bool(r.get("builtin", False)),
                last_sync=r.get("last_sync"),
            )
            for r in data
        ]

    async def _save_repos(self, repos: List[RepoConfig]) -> None:
        payload = json.dumps(
            [
                {
                    "id": r.id,
                    "name": r.name,
                    "index_url": r.index_url,
                    "verified": r.verified,
                    "builtin": r.builtin,
                    "last_sync": r.last_sync,
                }
                for r in repos
            ]
        )
        await self.db.set_setting(SETTINGS_KEY_REPOS, payload)

    async def add_repo(self, name: str, index_url: str) -> RepoConfig:
        if not index_url.startswith("https://"):
            raise PluginError("Seuls les dépôts HTTPS sont autorisés")
        # Valide en récupérant l'index
        index = await self._fetch_index(index_url)
        if not isinstance(index.get("plugins"), list):
            raise PluginError("index.json invalide (champ 'plugins' manquant)")

        repos = await self.list_repos()
        # Génère un ID slug unique
        base_id = _slugify(name)
        new_id = base_id
        counter = 2
        existing_ids = {r.id for r in repos}
        while new_id in existing_ids:
            new_id = f"{base_id}-{counter}"
            counter += 1

        repo = RepoConfig(
            id=new_id,
            name=name,
            index_url=index_url,
            verified=False,
            builtin=False,
            last_sync=int(datetime.now().timestamp()),
        )
        repos.append(repo)
        await self._save_repos(repos)
        self._catalog_cache[new_id] = (asyncio.get_event_loop().time(), index)
        return repo

    async def remove_repo(self, repo_id: str) -> None:
        repos = await self.list_repos()
        target = next((r for r in repos if r.id == repo_id), None)
        if target is None:
            raise PluginError(f"Dépôt '{repo_id}' introuvable")
        if target.builtin:
            raise PluginError(f"Le dépôt builtin '{repo_id}' ne peut pas être supprimé")
        repos = [r for r in repos if r.id != repo_id]
        await self._save_repos(repos)
        self._catalog_cache.pop(repo_id, None)

    async def refresh_repo(self, repo_id: str) -> int:
        repos = await self.list_repos()
        target = next((r for r in repos if r.id == repo_id), None)
        if target is None:
            raise PluginError(f"Dépôt '{repo_id}' introuvable")
        index = await self._fetch_index(target.index_url)
        plugin_count = len(index.get("plugins", []))
        self._catalog_cache[repo_id] = (asyncio.get_event_loop().time(), index)
        target.last_sync = int(datetime.now().timestamp())
        # Met à jour last_sync en DB
        await self._save_repos(repos)
        return plugin_count

    # ------------------------------------------------------------------
    # Catalog
    # ------------------------------------------------------------------

    async def fetch_catalog(self, repo_id: Optional[str] = None) -> List[dict]:
        """
        Retourne la liste agrégée des plugins disponibles dans tous les dépôts
        (ou un seul si repo_id fourni). Chaque entrée est annotée avec _repo_id
        et _repo_verified pour l'UI.
        """
        repos = await self.list_repos()
        if repo_id:
            repos = [r for r in repos if r.id == repo_id]

        results: List[dict] = []
        seen_ids: set = set()
        for repo in repos:
            try:
                index = await self._get_cached_index(repo)
            except Exception as e:
                logger.warning(
                    "Échec fetch catalog repo", repo_id=repo.id, error=str(e)
                )
                continue
            for entry in index.get("plugins", []):
                pid = entry.get("id")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                annotated = dict(entry)
                annotated["_repo_id"] = repo.id
                annotated["_repo_name"] = repo.name
                annotated["_repo_verified"] = repo.verified
                results.append(annotated)
        return results

    async def _get_cached_index(self, repo: RepoConfig) -> dict:
        loop = asyncio.get_event_loop()
        cached = self._catalog_cache.get(repo.id)
        if cached and (loop.time() - cached[0] < CATALOG_CACHE_TTL_SECONDS):
            return cached[1]
        index = await self._fetch_index(repo.index_url)
        self._catalog_cache[repo.id] = (loop.time(), index)
        return index

    async def _fetch_index(self, url: str) -> dict:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        if not isinstance(data, dict):
            raise PluginError("index.json: racine n'est pas un objet")
        return data

    # ------------------------------------------------------------------
    # Install / uninstall / toggle
    # ------------------------------------------------------------------

    async def install(
        self, plugin_id: str, repo_id: Optional[str], acknowledge_risk: bool
    ) -> PluginManifest:
        entry, repo = await self._find_catalog_entry(plugin_id, repo_id)
        if not repo.verified and not acknowledge_risk:
            raise PluginError(
                "Plugin non-officiel : confirmation explicite requise (acknowledge_risk=true)"
            )

        archive_url = entry.get("archive_url")
        if not archive_url or not archive_url.startswith("https://"):
            raise PluginError("archive_url manquante ou non-HTTPS")

        archive_subpath = entry.get("archive_subpath") or f"plugins/{plugin_id}"
        expected_checksum = entry.get("checksum_sha256")

        tmp_file = await self._download(archive_url)
        try:
            if expected_checksum:
                self._verify_checksum(tmp_file, expected_checksum)
            target = self.plugins_root / plugin_id
            if target.exists():
                shutil.rmtree(target)
            self._extract_subpath(tmp_file, archive_subpath, target)

            # Valide le manifest local
            manifest_path = target / "manifest.json"
            if not manifest_path.is_file():
                raise PluginError("Archive ne contient pas de manifest.json")
            with open(manifest_path, encoding="utf-8") as f:
                manifest = manifest_from_dict(json.load(f))
            if manifest.id != plugin_id:
                raise PluginError(
                    f"Manifest id='{manifest.id}' ne correspond pas à '{plugin_id}'"
                )
            if manifest.api_version != PLUGIN_API_VERSION:
                # On accepte l'install mais marquera en erreur au load
                logger.warning(
                    "Plugin installé avec api_version incompatible",
                    plugin_id=plugin_id,
                    api_version=manifest.api_version,
                    expected=PLUGIN_API_VERSION,
                )

            await self.db.plugin_upsert_record(
                plugin_id=plugin_id,
                version=manifest.version,
                source_type=manifest.source_type,
                source_repo=repo.id,
                enabled=True,
                installed=True,
                status="pending_restart",
                manifest_json=json.dumps(entry),
            )
            logger.success(
                "Plugin installé",
                plugin_id=plugin_id,
                version=manifest.version,
                repo=repo.id,
            )
            return manifest
        finally:
            try:
                tmp_file.unlink()
            except Exception:
                pass

    async def uninstall(self, plugin_id: str) -> None:
        record = await self.db.plugin_get_record(plugin_id)
        if record is None:
            raise PluginError(f"Plugin '{plugin_id}' non installé")
        target = self.plugins_root / plugin_id
        if target.exists():
            shutil.rmtree(target)
        await self.db.plugin_delete_record(plugin_id)
        logger.info("Plugin désinstallé", plugin_id=plugin_id)

    async def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        record = await self.db.plugin_get_record(plugin_id)
        if record is None:
            raise PluginError(f"Plugin '{plugin_id}' non installé")
        await self.db.plugin_set_enabled(plugin_id, enabled)

    # ------------------------------------------------------------------
    # Helpers (download, checksum, extract)
    # ------------------------------------------------------------------

    async def _find_catalog_entry(
        self, plugin_id: str, repo_id: Optional[str]
    ) -> Tuple[dict, RepoConfig]:
        repos = await self.list_repos()
        if repo_id:
            repos = [r for r in repos if r.id == repo_id]
            if not repos:
                raise PluginError(f"Dépôt '{repo_id}' introuvable")
        for repo in repos:
            try:
                index = await self._get_cached_index(repo)
            except Exception as e:
                logger.warning("Fetch repo échoué", repo_id=repo.id, error=str(e))
                continue
            for entry in index.get("plugins", []):
                if entry.get("id") == plugin_id:
                    return entry, repo
        raise PluginError(
            f"Plugin '{plugin_id}' introuvable dans les dépôts configurés"
        )

    async def _download(self, url: str) -> Path:
        timeout = aiohttp.ClientTimeout(total=120)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
        tmp_path = Path(tmp.name)
        tmp.close()
        total = 0
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            total += len(chunk)
                            if total > self.MAX_ARCHIVE_BYTES:
                                raise PluginError(
                                    f"Archive > {self.MAX_ARCHIVE_BYTES // (1024*1024)} MB (refus)"
                                )
                            f.write(chunk)
            return tmp_path
        except Exception:
            try:
                tmp_path.unlink()
            except Exception:
                pass
            raise

    def _verify_checksum(self, path: Path, expected: str) -> None:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                h.update(chunk)
        actual = h.hexdigest()
        if actual.lower() != expected.lower():
            raise PluginError(f"Checksum SHA-256 invalide (attendu {expected}, obtenu {actual})")

    def _extract_subpath(self, tarball: Path, subpath: str, dest: Path) -> None:
        """
        Extrait seulement subpath/ du tarball vers dest/, en aplatissant
        (dest/ contient directement les fichiers qui étaient dans archive_root/subpath/).

        GitHub tarballs wrappent tout dans un dossier racine (ex:
        p-streamrec-plugins-main/). On détecte ce wrapper et on le strip.
        """
        dest.mkdir(parents=True, exist_ok=True)
        subpath_norm = subpath.strip("/")
        with tarfile.open(tarball, "r:*") as tar:
            members = tar.getmembers()
            if not members:
                raise PluginError("Archive vide")

            # Détecte le wrapper dir (premier segment commun à tous les membres)
            first_segments = {
                m.name.split("/", 1)[0] for m in members if m.name
            }
            wrapper = next(iter(first_segments)) if len(first_segments) == 1 else ""

            prefix = f"{wrapper}/{subpath_norm}/" if wrapper else f"{subpath_norm}/"
            extracted = 0
            for m in members:
                if not m.name.startswith(prefix):
                    continue
                # Protection path traversal
                rel = m.name[len(prefix):]
                if not rel or rel.startswith("/") or ".." in rel.split("/"):
                    continue
                target = (dest / rel).resolve()
                if not str(target).startswith(str(dest.resolve())):
                    raise PluginError(f"Refus extraction hors-dest: {m.name}")
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif m.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    f = tar.extractfile(m)
                    if f is None:
                        continue
                    with open(target, "wb") as out:
                        shutil.copyfileobj(f, out)
                    extracted += 1
            if extracted == 0:
                raise PluginError(
                    f"Aucun fichier extrait (subpath '{subpath_norm}' introuvable dans l'archive)"
                )


def _slugify(text: str) -> str:
    out = []
    for c in text.lower().strip():
        if c.isalnum():
            out.append(c)
        elif c in " _-":
            out.append("-")
    slug = "".join(out).strip("-")
    return slug or "repo"

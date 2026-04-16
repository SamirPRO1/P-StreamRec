/* Plugins tab - install, uninstall, enable/disable, repos management. */

var _pluginsState = {
  loaded: false,
  catalog: [],
  installed: [],
  repos: []
};

document.addEventListener('DOMContentLoaded', function () {
  var navItem = document.querySelector('.settings-nav-item[data-tab="plugins"]');
  if (!navItem) return;
  navItem.addEventListener('click', function () {
    if (_pluginsState.loaded) return;
    _pluginsState.loaded = true;
    refreshPlugins();
  });
});

function refreshPlugins() {
  loadInstalledPlugins();
  loadRepos().then(loadCatalog);
}

// ---------------------------------------------------------------------------
// Fetch helpers
// ---------------------------------------------------------------------------

async function _api(method, url, body) {
  var opts = { method: method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  var res = await fetch(url, opts);
  var data = null;
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) {
    var msg = (data && (data.detail || data.message)) || ('HTTP ' + res.status);
    throw new Error(msg);
  }
  return data;
}

// ---------------------------------------------------------------------------
// Installed plugins
// ---------------------------------------------------------------------------

async function loadInstalledPlugins() {
  var container = document.getElementById('installedPluginsList');
  if (!container) return;
  try {
    var list = await _api('GET', '/api/plugins/installed');
    _pluginsState.installed = list;
    renderInstalledPlugins(list);
    var pending = list.some(function (p) { return p.status === 'pending_restart'; });
    var banner = document.getElementById('restartBanner');
    if (banner) banner.style.display = pending ? 'flex' : 'none';
  } catch (e) {
    container.innerHTML = '<p style="color: var(--danger); font-size: 0.9rem;">Failed to load: ' + _escape(e.message) + '</p>';
  }
}

function renderInstalledPlugins(list) {
  var container = document.getElementById('installedPluginsList');
  if (!list || list.length === 0) {
    container.innerHTML = '<p style="color: var(--text-muted); font-size: 0.9rem;">No plugins installed.</p>';
    return;
  }
  var html = '<div class="plugin-grid">';
  list.forEach(function (p) {
    html += _pluginCardHtml(p, true);
  });
  html += '</div>';
  container.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Catalog
// ---------------------------------------------------------------------------

async function loadCatalog() {
  var container = document.getElementById('pluginCatalog');
  var repoSelect = document.getElementById('repoFilter');
  var repoId = repoSelect ? repoSelect.value : '';
  if (!container) return;
  container.innerHTML = '<p style="color: var(--text-muted); font-size: 0.9rem;">Loading catalog...</p>';
  try {
    var qs = repoId ? ('?repo_id=' + encodeURIComponent(repoId)) : '';
    var list = await _api('GET', '/api/plugins/catalog' + qs);
    _pluginsState.catalog = list;
    renderCatalog(list);
  } catch (e) {
    container.innerHTML = '<p style="color: var(--danger); font-size: 0.9rem;">Failed to load catalog: ' + _escape(e.message) + '</p>';
  }
}

function renderCatalog(list) {
  var container = document.getElementById('pluginCatalog');
  if (!list || list.length === 0) {
    container.innerHTML = '<p style="color: var(--text-muted); font-size: 0.9rem;">No plugins found in configured repositories.</p>';
    return;
  }
  var installedIds = new Set(_pluginsState.installed.map(function (p) { return p.id; }));
  var html = '';
  list.forEach(function (p) {
    p._isInstalled = installedIds.has(p.id);
    html += _pluginCardHtml(p, false);
  });
  container.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Card rendering
// ---------------------------------------------------------------------------

function _pluginCardHtml(p, isInstalledView) {
  var badge = '';
  if (p.builtin) {
    badge = '<span class="plugin-badge builtin">Built-in</span>';
  } else if (p.verified || p.official) {
    badge = '<span class="plugin-badge verified">Official</span>';
  } else {
    badge = '<span class="plugin-badge community">Community</span>';
  }

  var statusPill = '';
  if (isInstalledView) {
    var cls = 'unknown', label = p.status;
    if (p.status === 'loaded') { cls = 'connected'; label = 'Loaded'; }
    else if (p.status === 'error') { cls = 'disconnected'; label = 'Error'; }
    else if (p.status === 'pending_restart') { cls = 'unknown'; label = 'Pending restart'; }
    else if (p.status === 'disabled') { cls = 'unknown'; label = 'Disabled'; }
    statusPill = '<span class="status-indicator ' + cls + '">' + _escape(label) + '</span>';
  }

  var toggle = '';
  if (isInstalledView && !p.builtin) {
    var checked = (p.enabled || p.status === 'loaded') ? 'checked' : '';
    toggle = (
      '<label class="toggle-switch" title="Enable/disable">'
      + '<input type="checkbox" ' + checked + ' onchange="togglePlugin(\'' + _escape(p.id) + '\', this.checked)" />'
      + '<span class="toggle-slider"></span>'
      + '</label>'
    );
  }

  var actionBtn = '';
  if (isInstalledView) {
    if (!p.builtin) {
      actionBtn = '<button class="btn-secondary btn-danger" onclick="uninstallPlugin(\'' + _escape(p.id) + '\')" style="padding: 0.35rem 0.75rem; font-size: 0.8rem;">Uninstall</button>';
    }
  } else {
    if (p._isInstalled) {
      actionBtn = '<button class="btn-secondary" disabled style="padding: 0.35rem 0.75rem; font-size: 0.8rem;">Installed</button>';
    } else {
      actionBtn = '<button class="btn-primary" onclick="installPlugin(\'' + _escape(p.id) + '\', ' + (p.source_repo ? ('\'' + _escape(p.source_repo) + '\'') : 'null') + ', ' + (p.verified ? 'true' : 'false') + ')" style="padding: 0.35rem 0.9rem; font-size: 0.8rem;">Install</button>';
    }
  }

  var errorLine = '';
  if (isInstalledView && p.last_error) {
    errorLine = '<p style="color: var(--danger); font-size: 0.75rem; margin-top: 0.25rem;">'
              + 'Error: ' + _escape(p.last_error) + '</p>';
  }

  var iconUrl = p.icon_url || ('/api/plugins/icon/' + p.id);

  return (
    '<div class="plugin-card" data-plugin-id="' + _escape(p.id) + '">'
    + '<img class="plugin-icon" src="' + _escape(iconUrl) + '" alt="' + _escape(p.name) + '" onerror="this.src=\'/api/plugins/icon/__fallback__\'" />'
    + '<div class="plugin-card-body">'
    +   '<div class="plugin-card-header">'
    +     '<h4>' + _escape(p.name || p.id) + '</h4>'
    +     '<span class="plugin-version">v' + _escape(p.version || '?') + '</span>'
    +     badge
    +   '</div>'
    +   '<p class="plugin-description">' + _escape(p.description || '') + '</p>'
    +   errorLine
    +   '<div class="plugin-card-footer">'
    +     statusPill
    +     toggle
    +     '<div style="margin-left: auto;">' + actionBtn + '</div>'
    +   '</div>'
    + '</div>'
    + '</div>'
  );
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function installPlugin(pluginId, repoId, isVerified) {
  if (!isVerified) {
    var confirmed = confirm(
      'WARNING: Community plugins run arbitrary Python code with full access to your server.\n\n' +
      'Only install plugins from authors you trust.\n\n' +
      'Install "' + pluginId + '" from non-verified repository?'
    );
    if (!confirmed) return;
  }
  try {
    var data = await _api('POST', '/api/plugins/install', {
      plugin_id: pluginId,
      repo_id: repoId || null,
      acknowledge_risk: !isVerified
    });
    showNotification(data.message || 'Plugin installed', 'success');
    await loadInstalledPlugins();
    await loadCatalog();
  } catch (e) {
    showNotification('Install failed: ' + e.message, 'error');
  }
}

async function uninstallPlugin(pluginId) {
  if (!confirm('Uninstall plugin "' + pluginId + '"? This cannot be undone.')) return;
  try {
    await _api('POST', '/api/plugins/uninstall/' + encodeURIComponent(pluginId));
    showNotification('Plugin uninstalled', 'success');
    await loadInstalledPlugins();
    await loadCatalog();
  } catch (e) {
    showNotification('Uninstall failed: ' + e.message, 'error');
  }
}

async function togglePlugin(pluginId, enabled) {
  var endpoint = enabled ? 'enable' : 'disable';
  try {
    await _api('POST', '/api/plugins/' + endpoint + '/' + encodeURIComponent(pluginId));
    showNotification('Plugin ' + (enabled ? 'enabled' : 'disabled') + '. Restart required.', 'success');
    await loadInstalledPlugins();
  } catch (e) {
    showNotification('Toggle failed: ' + e.message, 'error');
    await loadInstalledPlugins();
  }
}

async function restartForPlugins() {
  if (!confirm('Restart the application to apply plugin changes?')) return;
  try {
    await _api('POST', '/api/plugins/restart');
    showNotification('Restarting...', 'success');
    _pollRestart();
  } catch (e) {
    showNotification('Restart failed: ' + e.message, 'error');
  }
}

function _pollRestart() {
  var attempts = 0;
  var maxAttempts = 30;
  var interval = setInterval(function () {
    attempts++;
    if (attempts > maxAttempts) {
      clearInterval(interval);
      showNotification('Restart is taking longer than expected. Please refresh manually.', 'error');
      return;
    }
    fetch('/api/version', { signal: AbortSignal.timeout(3000) })
      .then(function (res) {
        if (res.ok) {
          clearInterval(interval);
          setTimeout(function () { window.location.reload(); }, 800);
        }
      })
      .catch(function () { /* still down */ });
  }, 2000);
}

// ---------------------------------------------------------------------------
// Repos
// ---------------------------------------------------------------------------

async function loadRepos() {
  var container = document.getElementById('reposList');
  var select = document.getElementById('repoFilter');
  try {
    var list = await _api('GET', '/api/plugins/repos');
    _pluginsState.repos = list;
    renderRepos(list);
    if (select) {
      var currentValue = select.value;
      while (select.options.length > 1) select.remove(1);
      list.forEach(function (r) {
        var opt = document.createElement('option');
        opt.value = r.id;
        opt.textContent = r.name + (r.verified ? ' (verified)' : '');
        select.appendChild(opt);
      });
      select.value = currentValue;
    }
  } catch (e) {
    if (container) {
      container.innerHTML = '<p style="color: var(--danger); font-size: 0.9rem;">Failed to load: ' + _escape(e.message) + '</p>';
    }
  }
}

function renderRepos(list) {
  var container = document.getElementById('reposList');
  if (!list || list.length === 0) {
    container.innerHTML = '<p style="color: var(--text-muted); font-size: 0.9rem;">No repositories configured.</p>';
    return;
  }
  var html = '';
  list.forEach(function (r) {
    var badge = r.verified
      ? '<span class="plugin-badge verified">Verified</span>'
      : '<span class="plugin-badge community">Community</span>';
    var removeBtn = r.builtin
      ? '<span style="color: var(--text-muted); font-size: 0.75rem;">Built-in</span>'
      : '<button class="btn-secondary btn-danger" onclick="removeRepo(\'' + _escape(r.id) + '\')" style="padding: 0.3rem 0.7rem; font-size: 0.75rem;">Remove</button>';
    html += (
      '<div style="display: flex; align-items: center; gap: 0.75rem; padding: 0.75rem 1rem; background: var(--bg-secondary); border: 1px solid var(--border); border-radius: 10px;">'
      + '<div style="flex: 1; min-width: 0;">'
      +   '<div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">'
      +     '<strong>' + _escape(r.name) + '</strong>'
      +     badge
      +   '</div>'
      +   '<div style="color: var(--text-muted); font-size: 0.75rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">' + _escape(r.index_url) + '</div>'
      + '</div>'
      + '<button class="btn-secondary" onclick="refreshRepo(\'' + _escape(r.id) + '\')" style="padding: 0.3rem 0.7rem; font-size: 0.75rem;">Refresh</button>'
      + removeBtn
      + '</div>'
    );
  });
  container.innerHTML = html;
}

async function addRepo(event) {
  event.preventDefault();
  var name = document.getElementById('newRepoName').value.trim();
  var url = document.getElementById('newRepoUrl').value.trim();
  if (!name || !url) return;
  try {
    await _api('POST', '/api/plugins/repos', { name: name, index_url: url });
    showNotification('Repository added', 'success');
    document.getElementById('newRepoName').value = '';
    document.getElementById('newRepoUrl').value = '';
    await loadRepos();
    await loadCatalog();
  } catch (e) {
    showNotification('Add repo failed: ' + e.message, 'error');
  }
}

async function removeRepo(repoId) {
  if (!confirm('Remove repository "' + repoId + '"?')) return;
  try {
    await _api('DELETE', '/api/plugins/repos/' + encodeURIComponent(repoId));
    showNotification('Repository removed', 'success');
    await loadRepos();
    await loadCatalog();
  } catch (e) {
    showNotification('Remove failed: ' + e.message, 'error');
  }
}

async function refreshRepo(repoId) {
  try {
    var data = await _api('POST', '/api/plugins/repos/' + encodeURIComponent(repoId) + '/refresh');
    showNotification('Refreshed (' + (data.plugin_count || 0) + ' plugins)', 'success');
    await loadCatalog();
  } catch (e) {
    showNotification('Refresh failed: ' + e.message, 'error');
  }
}

// ---------------------------------------------------------------------------
// Utils
// ---------------------------------------------------------------------------

function _escape(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

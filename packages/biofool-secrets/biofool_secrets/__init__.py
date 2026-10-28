"""biofool_secrets — canonical secret resolution for the biofool portfolio.

One implementation of the env → GCP Secret Manager → Proton Pass chain,
one KEY_ALIASES registry, consumed by every repo as a pinned git
dependency (CloudManagement#129). Replaces the diverged copies in
``secret_loader.py`` (CloudManagement / MultiCloud-MultiPass /
OSenseiDocuments), ``config/secrets.py`` (story_graph), and
``bootstrap_env.py`` (AIRichardMoon / WorldStudioFinder / VigilentSupport).

    from biofool_secrets import resolve_secret, list_secret_names

    key = resolve_secret("GEMINI_API_KEY")                     # chain resolution
    key = resolve_secret("GEMINI_API_KEY", project="story_graph")
    names = list_secret_names()                                # names only, never values

CLI: ``python -m biofool_secrets resolve NAME`` / ``list`` / ``env`` —
see ``python -m biofool_secrets --help``.
"""

from biofool_secrets.aliases import (
    aliases_for,
    all_projects,
    load_registry as load_alias_registry,
)
from biofool_secrets.deprecation import get_canonical, is_deprecated
from biofool_secrets.resolver import (
    backend,
    fetch_secret,
    get,
    invalidate_cache,
    is_enabled,
    list_secret_names,
    reset_caches,
    resolution_report,
    resolve_secret,
)

from biofool_secrets.token import resolve_token, token_candidates

__version__ = "0.1.0"

__all__ = [
    "aliases_for",
    "all_projects",
    "backend",
    "fetch_secret",
    "get",
    "get_canonical",
    "invalidate_cache",
    "is_deprecated",
    "is_enabled",
    "list_secret_names",
    "load_alias_registry",
    "reset_caches",
    "resolution_report",
    "resolve_secret",
    "resolve_token",
    "token_candidates",
    "__version__",
]

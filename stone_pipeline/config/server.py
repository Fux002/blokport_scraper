"""Process entry for the control-plane HTTP server (the /config/v1 admin surface).

This module is a DEPLOYED CONTRACT: the sync service task def runs it as
`python -m stone_pipeline.config.server` (infra/modules/sync_service/main.tf) and the CI
import smoke imports it (.github/workflows/deploy.yml). It must stay importable and runnable
under that path. It is a thin entry on purpose and re-exports NO names: the server itself
(ConfigHandler, dispatch, serve, ...) lives in stone_pipeline.config.control.server, so the
base config layer carries no edge into the control layer. No em dashes (design principle 2).
"""

from __future__ import annotations

if __name__ == "__main__":
    from stone_pipeline.config.control import server

    raise SystemExit(server.main())

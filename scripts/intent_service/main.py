#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from common.service_runtime import run_service_loop

try:
    from .intent_config import Config, Topics, load_config
    from .intent_routing import (
        IntentRouterEngine,
        LlmIntentClassifier,
        ManifestAliasResolver,
        MoonshineIntentMatcher,
        RouteDecision,
        new_corr_id,
        normalize,
    )
    from .intent_service_impl import IntentService, extract_identity_fields
except Exception:
    from intent_config import Config, Topics, load_config
    from intent_routing import (
        IntentRouterEngine,
        LlmIntentClassifier,
        ManifestAliasResolver,
        MoonshineIntentMatcher,
        RouteDecision,
        new_corr_id,
        normalize,
    )
    from intent_service_impl import IntentService, extract_identity_fields


def _extract_identity_fields(payload):
    return extract_identity_fields(payload)


def main() -> int:
    cfg = load_config()
    svc = IntentService(cfg)
    return run_service_loop(
        service_name="intent",
        start=svc.start,
        stop=svc.stop,
        interval_sec=0.5,
    )


if __name__ == "__main__":
    sys.exit(main())

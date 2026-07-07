# GENERATED — do not edit. Canonical source: analitiq-ai/infrastructure (alq-models layer), staged by contract-models/scripts/build.py on release.
import os

# The infra source keeps `os.environ['DOMAIN']` fail-loud for deploys; this
# public package validates PUBLIC connector documents, whose contract host is
# ALWAYS analitiq.ai. Force it (not setdefault) — an ambient DOMAIN=analitiq.dev
# (common in infra/dev shells) would otherwise build the `$schema` Literals for
# the wrong host and reject valid public docs. Matches the validator's own
# import-time behavior; runs before k2m.models.shared.common imports.
os.environ["DOMAIN"] = "analitiq.ai"

import os
import re

# Set at image build time from the release tag (e.g. "0.1.2"); "edge"/"main" for branch builds.
_v = (os.environ.get("NARRATARR_VERSION") or "").strip().lstrip("v")
_rev = (os.environ.get("NARRATARR_REVISION") or "").strip()[:7]
if re.fullmatch(r"\d+\.\d+\.\d+.*", _v):
    __version__ = _v
elif _v and _v not in ("dev",):
    __version__ = f"{_v} ({_rev})" if _rev else _v
else:
    __version__ = "dev"

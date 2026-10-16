"""orjson-backed JSON response (faster than the stdlib json encoder)."""

from __future__ import annotations

from typing import Any

import orjson
from starlette.responses import JSONResponse


class ORJSONResponse(JSONResponse):
    """JSONResponse rendered with orjson."""

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content, option=orjson.OPT_NON_STR_KEYS)

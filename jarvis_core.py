"""David's engine room: OpenJarvis runs the tools, Gemini Live still does the talking.

Every David action (open_app, click_on, check_email...) becomes an OpenJarvis
tool, so it gets timeouts, a bounded worker pool and events from the framework
instead of hand-rolled threads. The spoken confirmation gate stays in
david_live.py: it works across voice turns, which OpenJarvis's synchronous
confirm callback cannot.

PRIVACY: OpenJarvis ships with usage analytics ON by default (to a PostHog
server). David reads your screen and mail, so this module gives David its own
OpenJarvis home inside this folder and writes a config with analytics and
telemetry off BEFORE openjarvis is imported.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.join(HERE, ".openjarvis")
CONFIG = os.path.join(HOME, "config.toml")

_CONFIG_TEXT = """\
# Written by David (jarvis_core.py) on every start. Edit jarvis_core.py, not this.
[analytics]
enabled = false

[telemetry]
enabled = false
"""


def isolate():
    """Point OpenJarvis at David's own folder and switch its analytics off."""
    os.makedirs(HOME, exist_ok=True)
    with open(CONFIG, "w", encoding="utf-8") as f:
        f.write(_CONFIG_TEXT)
    os.environ["OPENJARVIS_HOME"] = HOME
    os.environ["OPENJARVIS_CONFIG"] = CONFIG


isolate()   # must run before any `import openjarvis`

from openjarvis.core.config import load_config                     # noqa: E402
from openjarvis.core.types import ToolCall, ToolResult             # noqa: E402
from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec  # noqa: E402


def privacy_report():
    """What OpenJarvis will and won't phone home. Used by --check and the tests."""
    cfg = load_config()
    return {"analytics": cfg.analytics.enabled, "telemetry": cfg.telemetry.enabled,
            "home": os.environ.get("OPENJARVIS_HOME")}


def _json_schema(node):
    """Gemini writes types as OBJECT/STRING; JSON Schema wants lower case."""
    if isinstance(node, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _json_schema(v))
                for k, v in node.items()}
    if isinstance(node, list):
        return [_json_schema(x) for x in node]
    return node


class DavidTool(BaseTool):
    """One David action, wrapped. Calls the existing function unchanged."""

    def __init__(self, decl, fn, *, timeout=30.0, destructive=False):
        self.tool_id = decl["name"]
        self._fn = fn
        self._spec = ToolSpec(
            name=decl["name"],
            description=decl.get("description", ""),
            parameters=_json_schema(decl.get("parameters") or {"type": "object", "properties": {}}),
            category="david",
            timeout_seconds=timeout,
            # David's own spoken gate decides; see the module docstring.
            requires_confirmation=False,
            metadata={"destructive": destructive},
        )

    @property
    def spec(self):
        return self._spec

    def execute(self, **params):
        out = self._fn(**params)
        panel = out if hasattr(out, "speech") else None
        text = out.speech if panel else ("" if out is None else str(out))
        return ToolResult(tool_name=self.tool_id, content=text, success=True,
                          metadata={"panel": panel} if panel else {})


class Engine:
    """Looks up David's actions and runs them through OpenJarvis's ToolExecutor."""

    def __init__(self, actions, decls, *, destructive=(), slow=(), screen=(), patient=()):
        by_name = {d["name"]: d for d in decls}
        self.tools = []
        for name in actions:
            decl = by_name.get(name) or {"name": name, "description": name}
            # Jobs that look at the screen or search the web legitimately take a while.
            # `patient` jobs wait on a person or a big copy (Gmail sign-in in a browser).
            timeout = (600.0 if name in patient
                       else 120.0 if (name in slow or name in screen) else 45.0)
            # looked up at call time, so swapping an entry in `actions` still takes effect
            tool = DavidTool(decl, lambda _n=name, **kw: actions[_n](**kw),
                             timeout=timeout, destructive=name in destructive)
            self.tools.append(tool)
        self.executor = ToolExecutor(self.tools)

    def names(self):
        return [t.spec.name for t in self.tools]

    def run(self, name, args):
        """Returns (text, panel_or_None). Raises RuntimeError if the tool failed."""
        res = self.executor.execute(ToolCall(id=name, name=name, arguments=json.dumps(args or {})))
        if not res.success:
            if "timed out" in res.content:
                # OpenJarvis cannot stop a running function, so it may still finish.
                raise RuntimeError("it took longer than expected and may still be running or "
                                   "may have finished. Check before asking again")
            raise RuntimeError(res.content)
        return res.content, res.metadata.get("panel")

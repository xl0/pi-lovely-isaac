"""Execute Python source in a persistent namespace: top-level await + Jupyter-style
last-expression value (if the last top-level statement is an expression, its value
is the result)."""

from __future__ import annotations

import ast
import dis
import traceback
from ast import PyCF_ALLOW_TOP_LEVEL_AWAIT

_COROUTINE_FLAG = next(k for k, v in dis.COMPILER_FLAG_NAMES.items() if v == "COROUTINE")


def format_exc_list(exc: BaseException) -> list[str]:
    try:
        return traceback.format_exception(type(exc), exc, exc.__traceback__)
    except Exception:
        return [f"{type(exc).__name__}: {exc}\n"]


class Executor:
    """One persistent namespace; async execution with cancellation at await points."""

    def __init__(self, namespace: dict | None = None) -> None:
        self.namespace: dict = namespace if namespace is not None else {}
        self._flags = PyCF_ALLOW_TOP_LEVEL_AWAIT

    async def run(self, source: str) -> tuple[bool, object]:
        """Execute source; returns (has_value, value).

        Body statements run first (awaited if they use top-level await), then the
        split-off trailing expression. Exceptions (incl. SyntaxError from parse)
        propagate to the caller; asyncio.CancelledError propagates from await
        points, which is what makes exec cancellation genuine.
        """
        tree = ast.parse(source, "<agent-exec>")
        expr = None
        last = tree.body[-1] if tree.body else None
        if isinstance(last, ast.Expr):
            tree.body.pop()
            expr = ast.Expression(last.value)

        if tree.body:
            code = compile(tree, "<agent-exec>", "exec", flags=self._flags, dont_inherit=True)
            res = eval(code, self.namespace, self.namespace)  # noqa: S307
            if code.co_flags & _COROUTINE_FLAG:
                await res
        if expr is None:
            return False, None
        code = compile(expr, "<agent-exec>", "eval", flags=self._flags, dont_inherit=True)
        value = eval(code, self.namespace, self.namespace)  # noqa: S307
        if code.co_flags & _COROUTINE_FLAG:
            value = await value
        return True, value

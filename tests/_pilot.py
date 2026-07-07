"""Shared helpers for driving Textual pilot tests without timing races.

Two distinct races bite the headless pilot tests, both surfacing as
``NoMatches`` (e.g. ``#state-body``, ``#debug-panels``):

* **start-side** — a test queries a widget right after a single
  ``pilot.pause()``, before the layout is mounted or the app's periodic
  refresh has populated it.  :func:`wait_for` / :func:`wait_until` pump the
  event loop until the condition the assertion needs actually holds.

* **teardown-side** — the app's ``set_interval(0.1, self._refresh)`` keeps
  ticking while ``run_test.__aexit__`` tears the screen down; a tick landing
  after the screen's widgets are removed raises ``NoMatches('#state-body')``
  *inside the app*, and Textual re-raises that stored exception at context
  exit (so the traceback points at ``async with app.run_test(...)``).
  :func:`run_app` stops the app-level interval timers once the test body is
  done, before the context manager exits.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

from textual.app import App
from textual.css.query import NoMatches
from textual.pilot import Pilot
from textual.widget import Widget

__all__ = ["run_app", "wait_for", "wait_until"]


async def wait_until(
    pilot: Pilot,
    predicate: Callable[[], bool],
    *,
    tries: int = 100,
    describe: str = "condition",
) -> None:
    """Pump ``pilot.pause()`` until ``predicate()`` is truthy.

    ``NoMatches`` raised by the predicate counts as "not yet" (the widget is
    simply not mounted yet); any other exception propagates immediately.
    Early attempts pause without a delay (cheap message pumping); later ones
    add a small real delay so interval-timer-driven updates can land too.
    Raises a clear :class:`AssertionError` if the condition never settles.
    """
    last_error: NoMatches | None = None
    for attempt in range(tries):
        try:
            if predicate():
                return
            last_error = None
        except NoMatches as error:
            last_error = error
        # After a burst of free pumps, let wall-clock time pass for timers.
        await pilot.pause(0.05 if attempt >= 20 else None)
    hint = f"; last query error: {last_error}" if last_error is not None else ""
    raise AssertionError(
        f"pilot never settled: {describe} still false after {tries} pauses{hint}"
    )


async def wait_for(
    pilot: Pilot,
    app: App,
    selector: str,
    *,
    ready: Callable[[Any], bool] = lambda widget: True,
    tries: int = 100,
) -> Widget:
    """Wait until ``app.query_one(selector)`` exists and ``ready(widget)`` holds.

    Returns the widget so the caller can assert on its settled state.
    """
    found: Widget | None = None

    def check() -> bool:
        nonlocal found
        found = app.query_one(selector)
        return bool(ready(found))

    await wait_until(pilot, check, tries=tries, describe=f"{selector} ready")
    assert found is not None  # for the type checker; wait_until guarantees it
    return found


@asynccontextmanager
async def run_app(app: App, *, size: tuple[int, int]) -> AsyncIterator[Pilot]:
    """``app.run_test(size=...)`` with a race-free teardown.

    The app never stops its ``set_interval`` refresh timer itself (harmless
    in production, where the process exits with the app), so a tick can fire
    mid-teardown after the screen's widgets are gone and crash the app with
    ``NoMatches``.  Stop the app-level timers once the test body finishes,
    then let the context manager shut the app down.
    """
    async with app.run_test(size=size) as pilot:
        try:
            yield pilot
        finally:
            for timer in list(app._timers):  # noqa: SLF001 - test-only teardown aid
                timer.stop()
            await pilot.pause()

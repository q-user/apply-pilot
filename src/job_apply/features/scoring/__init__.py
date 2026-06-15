"""Scoring vertical slice (M3).

This slice owns everything that turns a :class:`VacancyMatch` into a
ranking the user can review. Issue #30 (this PR) introduces:

* the :class:`VacancyMatch` extension with ``explanation``,
  ``prompt_version`` and ``scored_at`` columns;
* a small in-memory + SQL :class:`PromptVersionRegistry` that future
  scoring passes (issue #29) will consult to pick the active prompt
  template for a given scoring task.

The actual LLM scoring engine lives in upcoming slices and is not part
of this module.
"""

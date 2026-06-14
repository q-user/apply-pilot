"""Audit log vertical slice.

Provides append-only event logging as a cross-cutting concern consumed
by other slices (auth, resumes, telegram, search) via DI.
"""

"""Answering questions from the Laravel database (text-to-SQL).

The model writes a SELECT; ``sql_guard`` parses it and rewrites every table reference
into a subquery that exposes only the columns and rows the requester may see, so the
security boundary is the rewrite, not the prompt.
"""

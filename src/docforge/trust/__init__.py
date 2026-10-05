"""The trust layer: checks that decide whether an extracted value can be relied on.

Nothing here calls a model. Verification compares each value with the source text it cites,
rules apply arithmetic and format checks, matching compares documents with each other.
"""

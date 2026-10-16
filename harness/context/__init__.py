"""Context and engagement dynamics: data about a post that the X API does not return.

Modules:
- `snowflake`: exact creation time from any X status or user ID.
- `xreader`: read-only X reader interface (`XReader`) and a keyless implementation.
- `http`: the polite, cached GET client every collector uses.

Nothing in this package writes to X. Collectors raise `http.FetchError` with the
reason when a source cannot be fetched; they never return an empty result to hide it.
See `data/context/DESIGN.md` for the feature dictionary.
"""

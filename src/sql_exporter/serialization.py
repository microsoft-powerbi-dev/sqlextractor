"""Pipe-delimited CSV dialect. CR/LF are preserved inside quoted fields."""
from .common import require


def record(values, null_token="\\N"):
    fields = []
    for value in values:
        if value is None:
            fields.append(null_token)
            continue
        require(isinstance(value, str), "Serializer expects formatted strings or nulls")
        # Reject collisions even when quoted, so ordinary CSV readers can decode NULL unambiguously.
        require(value != null_token, "Non-null value collides with null_token; choose another token in a new output directory")
        if any(c in value for c in '|"\r\n') or value == "":
            value = '"' + value.replace('"', '""') + '"'
        fields.append(value)
    return ("|".join(fields) + "\n").encode("utf-8")


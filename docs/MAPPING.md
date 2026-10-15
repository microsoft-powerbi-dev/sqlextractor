# Mapping contract v1

The workbook is executable metadata for a supported operation set. Arbitrary prose, Python, SQL expressions, Excel formulas, macros, and external links are not a transformation language. Unsupported rules fail before opening a source connection.

Engine 1.1 also accepts canonical `.json` mappings through the same validator. Optional background `mapping-job` preprocessing translates general documents into proposed canonical JSON; the export engine still executes only the validated declarative contract. See `DEVELOPER_GUIDE.md` for that workflow.

Start from `examples/mapping.xlsx`. The amber cells are editable inputs; `nullable` cells are native Excel booleans. Keep exactly two sheets, `Columns` and `Metadata`, with the exact headers below on their first nonempty row. Blank rows and trailing empty cells are ignored; hidden rows are still read. Do not add documentation columns or tabs; put longer rule rationale in version control alongside the workbook.

## Metadata

| key | value |
| --- | --- |
| contract_version | Numeric `1` |
| mapping_id | Logical identifier, e.g. `orders` |
| mapping_version | Text `MAJOR.MINOR.PATCH`, e.g. `1.0.0` |

Every key is required exactly once. Increment mapping_version for any intended contract/output change. Even if you forget to increment it, the canonical SHA-256 changes when any column/rule changes, and resume refuses to mix it with an existing export. Contract version describes engine semantics; mapping version describes this dataset's output definition.

## Columns

Exact header order: `ordinal`, `source`, `target`, `type`, `nullable`, `rules`, `scale`.

| Column | Contract |
| --- | --- |
| ordinal | Unique contiguous integer starting at 1; defines output order independently of workbook row order. |
| source | Source column identifier, or blank for a constant. A source may feed multiple targets. |
| target | Unique output header, case-insensitive uniqueness. |
| type | `string`, `integer`, `decimal`, `date`, `datetime`, or `boolean`. |
| nullable | Excel TRUE/FALSE boolean, not text `"true"` or `"false"`. Checked after transformations. |
| rules | Blank/`[]` for identity, otherwise a JSON array of supported operations applied left to right. Maximum 32 operations. |
| scale | Required integer 0–18 for decimal; blank for every other type. |

Source, target, mapping_id, schema/table/date names, and ordering identifiers must match `[A-Za-z_][A-Za-z0-9_]{0,127}`. Broader SQL identifier syntax is deliberately unsupported. Source names must match SQL metadata case exactly, even on a case-insensitive database.

The engine starts with `row[source]` (or null for a generated column), applies rules in order, then validates/converts/formats to the final target type. Except `constant`, `identity`, `template`, and `default`, operations pass null through unchanged. String operations require a string at that step; there is no hidden coercion. Empty text differs from null. A blank source requires a first `constant`, `identity`, or `template` rule.

## Supported rules

Only the listed keys are accepted. JSON duplicate keys and non-finite numbers are rejected. JSON scalars mean string, number, boolean, or null; not arrays/objects.

| Operation | Example | Behavior |
| --- | --- | --- |
| trim | `{"op":"trim"}` | Remove Unicode whitespace from both ends. |
| upper | `{"op":"upper"}` | Python Unicode uppercase. |
| lower | `{"op":"lower"}` | Python Unicode lowercase. |
| replace | `{"op":"replace","old":"-","new":""}` | Literal, case-sensitive replacement of all occurrences. No regex. |
| default | `{"op":"default","value":"UNKNOWN"}` | Replace null only; preserve empty text, zero, and false. |
| constant | `{"op":"constant","value":"ERP"}` | Override input with a scalar; must be first. |
| multiply | `{"op":"multiply","factor":"1.20"}` | Exact decimal multiplication. Decimal factors should be JSON strings. Binary float input/factors are rejected. |
| lookup | `{"op":"lookup","values":{"A":"ACTIVE"},"on_missing":"error"}` | Exact case-sensitive string lookup. Missing policy is required: `error`, `keep` original string, or `null`. Null input passes through. |
| to_string | `{"op":"to_string"}` | Explicit supported scalar-to-text conversion before string rules. |
| prefix | `{"op":"prefix","value":"AW-"}` | Prepend literal text, max 4096 characters, to non-null text. |
| suffix | `{"op":"suffix","value":"-WEB"}` | Append literal text, max 4096 characters, to non-null text. |
| identity | `{"op":"identity","namespace":"adventureworks/order","keys":["SalesOrderID"]}` | First operation only; generate deterministic UUIDv5 from namespace and ordered source keys. |
| template | `{"op":"template","value":"{SalesOrderID}/{SalesOrderDetailID}"}` | First operation only; build text from source fields. Plain identifier placeholders only; no format specs/expressions/attribute access. `{{` and `}}` escape braces. Missing/null values fail. |

Identity namespaces are explicit strings of 1–200 characters. Keys are 1–16 distinct source identifiers whose row values must be nonempty strings or integers (no null, bool, Decimal, or float). The namespace UUID is UUIDv5(NAMESPACE_URL, namespace). The result is UUIDv5(namespace UUID, canonical JSON of ordered `[type-tag,value]` pairs); tags are `integer` and `string`. Column names are excluded so differently named parent/foreign-key source columns can match. Namespace, key order, types, and values determine the result. It is independent of window, row order, and restart; it is not sequential. Source schema/key normalization remains the developer's responsibility. Multiple identity columns are simply additional mapping rows.

Example Customer rule cell:

```json
[{"op":"default","value":"UNKNOWN"},{"op":"trim"},{"op":"upper"}]
```

Example quantity conversion followed by final decimal formatting:

```json
[{"op":"multiply","factor":"0.001"}]
```

This mapping may convert grams to kilograms when that is the approved business rule. Unit semantics are the mapping owner's responsibility. `type=decimal, scale=3` controls final output precision; multiplication itself does not round to that scale.

## Final conversion

- `string`: accept text, integer, Decimal, boolean, date/datetime; date and datetime use ISO formatting. Other objects, bytes, and binary floats are rejected. Boolean-to-string follows Python `True`/`False`; choose `type=boolean` for lowercase boolean output. Use lookup for custom categorical labels.
- `integer`: exact signed 64-bit integer from integer, Decimal, or numeric text. Fractional input and overflow fail; there is no truncation.
- `decimal`: exact integer/Decimal/numeric text, at most 38 significant digits and magnitude exponent at most 38. No bool/float/NaN/infinity. Intermediate decimal operations use precision 80; output rounds with ROUND_HALF_UP to scale 0–18 and must fit 38 digits. Negative zero may retain its sign. Numeric JSON decimal literals become floats, so write them as strings.
- `date`: ISO string or Python date/datetime, normalize an offset timestamp to UTC first, then take the calendar date. This intentionally drops time.
- `datetime`: ISO string or Python date/datetime, UTC normalization, six fractional digits. Naive values mean UTC. Input precision beyond six digits fails. Selected SQL datetime2 scale greater than 6 is rejected before reading rows.
- `boolean`: native bool, integer 0/1, or exact lowercase strings `true`, `false`, `0`, `1`. Other spellings fail.

For a nullable target, a final null is serialized using null_token. For a non-nullable target, it fails the window. To guarantee a default after an operation that can produce null, place `default` after that operation.

Validation checks rule structure and parameters before connection. Whether a source value satisfies a rule is necessarily checked while streaming; `validate` does not sample or profile database rows. A bad source type or unmapped lookup fails at the first offending row, with its target name and prior successful row count in the CLI error.

## Workbook import and change control

Both `.xlsx` (openpyxl) and binary `.xls` (xlrd) normalize to the same contract. The XLS reader also scans BIFF formula records because xlrd otherwise exposes formula caches. XLSX formulas/error cells are rejected even if they have cached values. Limits: 10 MiB workbook file, 50 MiB expanded XLSX, 4,096 rows and 32 columns per sheet, 1–1,024 mapped targets. Styles and physical row order are excluded from the semantic hash; every metadata field and normalized column definition is included.

Review a mapping diff by comparing the normalized `mapping` object in run manifests, and keep mapping workbooks/configs under version control. Adding, renaming, reordering, casting, defaulting, and supported rule/lookup changes need only a workbook update and a new output directory. Joins, cross-row calculations, arbitrary expressions, and new operations require a deliberate implementation extension and engine/contract version decision; there is no `eval` escape hatch.

## Source queries and target layouts (engine 1.3)

The mapping contract above does not change. Two related inputs sit beside it:

- **Source query.** A config may name one read-only `SELECT` (`query` or `query_file`) instead of
  `schema`/`table`. Mapping `source` names refer to the query's output column names. SQL never
  comes from a workbook, a mapping, or AI output.
- **Target spec.** A target spec lists the required output columns (name, order, type,
  nullability, scale) and the file layout (pattern, extension, delimiter, header, line ending,
  encoding, null token). It can be imported from an XSD, an Excel `Target` sheet, a JSON spec, or
  a sample file. A mapping fits a spec when it has the same column names in the same order, the
  same types and scales, and no nullable column where the spec requires a value.
  `targets.check_against_target` reports every difference. Studio and the Devin provider enforce
  this before approval. See [STUDIO.md](STUDIO.md).

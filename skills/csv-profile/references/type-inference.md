# Type inference rules

Applied per cell, first match wins:

| Order | Type       | Rule                                                        |
|-------|------------|-------------------------------------------------------------|
| 1     | `integer`  | `^[+-]?\d+$`                                                 |
| 2     | `float`    | decimal or scientific notation                               |
| 3     | `datetime` | starts with `YYYY-MM-DD`, optional `T`/space and `HH:MM[:SS]` |
| 4     | `boolean`  | one of true/false/yes/no/y/n/t/f/0/1, case-insensitive        |
| 5     | `string`   | everything else                                              |

The column's `inferred_type` is the most frequent cell type among non-empty
cells. When more than one type occurs, the report includes a `type_mix` field
with the full breakdown — treat any column with a `type_mix` as dirty.

## Known sharp edges

- `0` and `1` match `integer` before `boolean`, so a genuine boolean column
  encoded numerically is reported as `integer`. Check `distinct == 2`.
- Leading zeros (`00123`, zip codes, phone numbers) are reported as `integer`
  and will lose the zeros if you cast on that basis. Check `samples`.
- Empty and whitespace-only cells both count as null; there is no distinction
  between "missing" and "empty string".
- Dates in `DD/MM/YYYY` or `MM/DD/YYYY` are reported as `string`, because the
  two are indistinguishable without knowing the locale.

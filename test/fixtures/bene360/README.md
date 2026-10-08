# Vendored Beneficiary-360 schemas

Unmodified copies of the published bene-360 JSON Schemas (Draft 2020-12), used by
`test/unit/test_bene360_schema.py` and `test/unit/test_fourth_registry.py` to check that the
requests this service accepts and the responses it delivers conform to the specification.

| File | Source |
|---|---|
| `request.schema.json` | https://github.com/OpenG2P/bene-360-api/blob/64178b295af26a313a7dc36a327d2cd68f260dd4/request.schema.json |
| `response.schema.json` | https://github.com/OpenG2P/bene-360-api/blob/64178b295af26a313a7dc36a327d2cd68f260dd4/response.schema.json |

Last commit touching both files: `64178b295af26a313a7dc36a327d2cd68f260dd4` (2026-08-20) on
`develop`, unchanged at branch head `08e7e34d2e0139ba148f450f70b9d677ba58a9d8`.

To update, download the files from the new commit, replace them here, update this table and
re-run `pytest test/unit`.

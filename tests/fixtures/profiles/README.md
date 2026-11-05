# Import-profile fixtures (V2 P3)

`SAMPLE-synthetic-purchase-import.csv` is **made up**: synthetic bills (`tests/profile_fixtures.py`)
in the built-in SAMPLE layout, typed by hand. It is not a Marg import file and no layout here is
a Marg format. When the pilot supplies a purchase import file its Marg accepted, add it here
(values replaced with synthetic ones) with the bills it books, and a golden test beside
`test_the_engine_reproduces_the_sample_file_cell_for_cell`.

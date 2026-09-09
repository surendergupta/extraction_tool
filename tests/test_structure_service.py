from app.services.structure import parse_structure


def test_parse_structure_empty_text():
    assert parse_structure(None) == {"sections": [], "tables": []}
    assert parse_structure("") == {"sections": [], "tables": []}


def test_parse_structure_sections_and_headers():
    text = "HISTORY\nPatient is stable.\n\nDIAGNOSIS:\nMild hypertension."
    result = parse_structure(text)
    titles = [s["title"] for s in result["sections"]]
    assert titles == ["HISTORY", "DIAGNOSIS"]
    assert result["sections"][0]["content"] == "Patient is stable."
    assert result["sections"][1]["content"] == "Mild hypertension."


def test_parse_structure_detects_table_block():
    text = "LABS\nTest   Result\nWBC    7.2\nHgb    13.1"
    result = parse_structure(text)
    assert len(result["tables"]) == 1
    rows = result["tables"][0]["rows"]
    assert rows[0] == ["Test", "Result"]
    assert rows[1] == ["WBC", "7.2"]


def test_parse_structure_pipe_delimited_table():
    text = "Test | Result\nWBC | 7.2"
    result = parse_structure(text)
    assert result["tables"][0]["rows"] == [["Test", "Result"], ["WBC", "7.2"]]


def test_parse_structure_defaults_to_general_section_when_no_header():
    text = "Just a plain note with no headers."
    result = parse_structure(text)
    assert result["sections"] == [{"title": "General", "content": "Just a plain note with no headers."}]


def test_parse_structure_uses_native_tables_when_available():
    """Native-PDF text collapses column spacing (e.g. 'Test  Result' becomes
    'Test Result' with a single space), so the space-heuristic never fires -
    pdfplumber's own extract_tables() output must be used directly instead."""
    text = "LABS\nTest Result\nWBC 7.2"  # single spaces - heuristic would find no table here
    native_tables = [
        [
            ["Test Name", "Status", "Result", "Reference Interval", "Unit"],
            ["Haemoglobin", None, "11.4", "12.0-15.0", "g/dL"],
        ]
    ]

    result = parse_structure(text, native_tables=native_tables)

    assert len(result["tables"]) == 1
    table = result["tables"][0]
    assert table["source"] == "native_pdf"
    assert table["rows"] == [
        ["Test Name", "Status", "Result", "Reference Interval", "Unit"],
        ["Haemoglobin", "", "11.4", "12.0-15.0", "g/dL"],  # None -> ""
    ]
    assert table["raw_lines"][0] == "Test Name | Status | Result | Reference Interval | Unit"
    # section text is untouched by the presence of native_tables - and
    # "WBC 7.2" must NOT become its own bogus section (see
    # test_parse_structure_data_row_with_allcaps_abbreviation_not_a_heading):
    # its letters ("WBC") are all-uppercase, but it carries a digit.
    assert [s["title"] for s in result["sections"]] == ["LABS"]


def test_parse_structure_heuristic_tables_tagged_with_source():
    text = "LABS\nTest   Result\nWBC    7.2"
    result = parse_structure(text)
    assert result["tables"][0]["source"] == "heuristic"


def test_parse_structure_no_native_tables_falls_back_to_heuristic():
    """A native-PDF document with no table on it (native_tables=[]) must not
    fabricate a table - and must behave identically to the no-argument case."""
    text = "IMPRESSION:\nNormal chest x-ray, no table content here."
    assert parse_structure(text, native_tables=[]) == parse_structure(text)
    assert parse_structure(text, native_tables=[])["tables"] == []
    assert parse_structure(text, native_tables=None)["tables"] == []


def test_parse_structure_drops_malformed_native_table_without_crashing():
    text = "FINDINGS:\nSome text."
    native_tables = [
        [["A", "B"], ["1", "2", "3"]],  # ragged row - malformed, must be dropped
        [],  # empty table - malformed, must be dropped
        [["Test", "Result"], ["WBC", "7.2"]],  # well-formed - must survive
    ]

    result = parse_structure(text, native_tables=native_tables)

    assert len(result["tables"]) == 1
    assert result["tables"][0]["rows"] == [["Test", "Result"], ["WBC", "7.2"]]


def test_parse_structure_drops_single_column_native_table():
    """Real false-positive pattern (confirmed on LabReport-1.pdf): a
    paragraph-text block bounded by decorative rules gets detected by
    pdfplumber as a "table" with exactly 1 column - that's never real
    tabular data on that document (genuine tables there had 5 columns), so
    it must be dropped rather than surfaced as structured_data."""
    text = "FINDINGS:\nSome text."
    single_column_false_positive = [
        ["Multiple fibrotic and fibrocalcific opacities are seen in the upper lobes."],
        ["Note:- This report IS NOT valid for medico-legal purposes."],
    ]

    result = parse_structure(text, native_tables=[single_column_false_positive])

    assert result["tables"] == []


def test_parse_structure_drops_single_row_native_table():
    text = "FINDINGS:\nSome text."
    single_row = [["Test Name", "Status", "Result"]]

    result = parse_structure(text, native_tables=[single_row])

    assert result["tables"] == []


def test_parse_structure_keeps_minimal_2x2_native_table():
    """The row/column floor is >=2 x >=2, not stricter than that - a small
    but genuine table shouldn't be rejected just for being small."""
    text = "LABS:\nSome text."
    native_tables = [[["Test", "Result"], ["WBC", "7.2"]]]

    result = parse_structure(text, native_tables=native_tables)

    assert len(result["tables"]) == 1
    assert result["tables"][0]["rows"] == [["Test", "Result"], ["WBC", "7.2"]]


def test_parse_structure_drops_letterhead_row_keeps_subheading_and_data_rows():
    """Row-level regression test (real pattern from LabReport-1.pdf's table
    1): within an otherwise-genuine multi-column table, a dense multi-line
    single-cell row (a repeated page letterhead) must be dropped, while a
    short single-line single-cell row (a legitimate section-subheading,
    e.g. "Differential Leucocyte Count" on that document) and all
    multi-cell data rows survive untouched."""
    text = "LABS:\nSome text."
    letterhead = "Lab No.: 555 UHID: AB/1\nPatient Name: Jane Doe\nDoctor: Dr. Smith"
    native_table = [
        [letterhead, None, None],
        ["Haemoglobin", "L", "11.4"],
        ["TLC", None, "9.6"],
        ["Differential Count", None, None],  # legitimate subheading - kept
    ]

    result = parse_structure(text, native_tables=[native_table])

    assert len(result["tables"]) == 1
    rows = result["tables"][0]["rows"]
    assert len(rows) == 3  # letterhead row dropped, the other 3 survive
    assert not any("Lab No." in cell for row in rows for cell in row)
    assert rows[0] == ["Haemoglobin", "L", "11.4"]
    assert rows[1] == ["TLC", "", "9.6"]
    assert rows[2] == ["Differential Count", "", ""]


def test_parse_structure_method_name_line_stays_attached_not_a_heading():
    """Real pattern from LabReport-1.pdf's flat text (space-heuristic path):
    a short ALL-CAPS method-name annotation ("COLORIMETRIC", "CALCULATED",
    "ELECTRICAL IMPEDENCE", "FLOW CYTOMETRY") sits on its own line directly
    under a test-result line. It has no digits itself but always follows a
    line that does - that must keep it out of heading detection and attached
    to the test-name content above it, not split into its own section."""
    text = (
        "COMPLETE HEMOGRAM\n"
        "Haemoglobin (Hb) * L 11.4* 12.0-15.0 g/dL\n"
        "COLORIMETRIC\n"
        "TLC (Total Leucocyte Count) * 9.6 4-10 10³/mm³\n"
        "ELECTRICAL IMPEDENCE\n"
        "MCV * L 82.0* 83-101 fL\n"
        "CALCULATED"
    )

    result = parse_structure(text)

    titles = [s["title"] for s in result["sections"]]
    assert titles == ["COMPLETE HEMOGRAM"]
    content = result["sections"][0]["content"]
    assert "Haemoglobin (Hb) * L 11.4* 12.0-15.0 g/dL\nCOLORIMETRIC" in content
    assert "ELECTRICAL IMPEDENCE" in content
    assert "CALCULATED" in content


def test_parse_structure_data_row_with_allcaps_abbreviation_not_a_heading():
    """Real pattern from the same document: a result row whose own test-name
    abbreviation happens to be all-uppercase (e.g. "RDW-CV", "ATYPICAL
    CELLS") reads as an all-caps line by the old rule even though it's a
    data row, not a heading - it carries a digit itself (the result value),
    which is what actually distinguishes it."""
    text = "COMPLETE HEMOGRAM\nRDW-CV H 15.7* 11.5-14.5 %\nATYPICAL CELLS 00"

    result = parse_structure(text)

    titles = [s["title"] for s in result["sections"]]
    assert titles == ["COMPLETE HEMOGRAM"]
    assert "RDW-CV H 15.7* 11.5-14.5 %" in result["sections"][0]["content"]
    assert "ATYPICAL CELLS 00" in result["sections"][0]["content"]


def test_parse_structure_genuine_heading_after_blank_line_still_detected():
    """The digit-adjacency check must not leak across a blank line (a
    natural paragraph/page break - see the real document's own page
    joins): a genuine heading opening a new block right after a
    digit-containing line, separated by a blank line, must still be
    detected as a heading."""
    text = "Result: 12.9 fl\n\nFINDINGS:\nEverything looks normal."

    result = parse_structure(text)

    titles = [s["title"] for s in result["sections"]]
    assert "FINDINGS" in titles


def test_parse_structure_genuine_short_allcaps_heading_still_works():
    """Sanity check: a short ALL-CAPS heading with no nearby digits at all
    must still be detected - the fix only targets digit-adjacent lines."""
    text = "DIAGNOSIS\nPatient is stable with no acute findings."

    result = parse_structure(text)

    assert result["sections"][0]["title"] == "DIAGNOSIS"


def test_parse_structure_uses_ruled_line_regions_on_ocr_path():
    """WP-D: on the rasterize+OCR path, table content comes from ruled-line
    table *regions* (bbox + region_text, NO grid). Entry shape differs from
    native_pdf entries - a consumer must branch on `source`."""
    text = "MEDICAL EXAMINATION REPORT\nsome body text here"
    regions = [
        {"bbox": [32, 427, 715, 883], "region_text": "Type of Examination Results\nEYE 6/6\nB.P. 120/80"},
        {"bbox": None, "region_text": "   "},          # blank - dropped
        {"not": "a dict"},                             # malformed - dropped
    ]

    result = parse_structure(text, ruled_line_regions=regions, space_heuristic_tables=False)

    assert len(result["tables"]) == 1
    t = result["tables"][0]
    assert t["source"] == "ruled_line_region"
    assert t["bbox"] == [32, 427, 715, 883]
    assert "B.P. 120/80" in t["region_text"]
    assert "rows" not in t and "raw_lines" not in t     # deliberately NOT a grid


def test_parse_structure_ocr_path_disables_space_heuristic_tables():
    """With space_heuristic_tables=False and no ruled-line regions (e.g. a
    borderless-table or table-free scanned page), NO table is fabricated -
    and the space-aligned lines are preserved as section content, not
    silently dropped into a discarded table buffer."""
    text = "RESULTS\nHaemoglobin    11.4 g/dL\nWBC    9.6\nPlatelets    2.1"

    result = parse_structure(text, space_heuristic_tables=False)

    assert result["tables"] == []
    content = result["sections"][0]["content"]
    assert "Haemoglobin    11.4 g/dL" in content and "WBC    9.6" in content


def test_parse_structure_native_tables_win_over_ruled_regions():
    """Defensive: if both are somehow supplied, native_pdf grids win."""
    result = parse_structure(
        "X",
        native_tables=[[["A", "B"], ["1", "2"]]],
        ruled_line_regions=[{"bbox": [0, 0, 1, 1], "region_text": "should be ignored"}],
    )
    assert len(result["tables"]) == 1
    assert result["tables"][0]["source"] == "native_pdf"


def test_parse_structure_drops_whole_table_when_too_few_rows_survive_filter():
    """A table can be well-formed (passes the 2x2 shape floor) yet still
    reduce to a single surviving row once its one spurious row is dropped -
    that single row is below the table's own row floor, so the whole table
    must be dropped rather than surface a degenerate 1-row result."""
    text = "LABS:\nSome text."
    letterhead = "Lab No.: 555\nPatient Name: Jane Doe"
    native_table = [
        [letterhead, None],  # spurious - dropped by row filtering
        ["Only Row", "X"],  # genuine, but alone that's < 2 rows
    ]

    result = parse_structure(text, native_tables=[native_table])

    assert result["tables"] == []

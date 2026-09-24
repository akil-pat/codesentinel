"""Tests for core.diff, against real `git diff` output (not hand-crafted
diff text) — hunk headers are fiddly enough to get wrong by hand that a
hand-written fixture would test the fixture more than the parser.
"""

from __future__ import annotations

import pytest
from unidiff.errors import UnidiffParseError

from codesentinel.core.diff import parse_diff

MODIFY_AND_ADD_DIFF = """\
diff --git a/src/new_file.py b/src/new_file.py
new file mode 100644
index 0000000..ec0acfb
--- /dev/null
+++ b/src/new_file.py
@@ -0,0 +1,2 @@
+def brand_new():
+    pass
diff --git a/src/util.py b/src/util.py
index c961442..70a4c1a 100644
--- a/src/util.py
+++ b/src/util.py
@@ -1,2 +1,3 @@
 def helper():
-    return 42
+    return 43
+    # tweaked
"""

RENAME_ONLY_DIFF = """\
diff --git a/src/old_name.py b/src/renamed.py
similarity index 100%
rename from src/old_name.py
rename to src/renamed.py
"""

DELETE_DIFF = """\
diff --git a/src/new_file.py b/src/new_file.py
deleted file mode 100644
index ec0acfb..0000000
--- a/src/new_file.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def brand_new():
-    pass
"""


def test_parse_diff_add_and_modify():
    parsed = parse_diff(MODIFY_AND_ADD_DIFF)

    assert [f.path for f in parsed.files] == ["src/new_file.py", "src/util.py"]
    assert parsed.warnings == ()

    added_file, modified_file = parsed.files
    assert added_file.is_added is True
    assert added_file.added_line_numbers == (1, 2)
    assert added_file.removed_line_numbers == ()

    assert modified_file.is_added is False
    assert modified_file.is_removed is False
    # line 2 ("return 42") removed, lines 2-3 ("return 43", "# tweaked") added
    assert modified_file.removed_line_numbers == (2,)
    assert modified_file.added_line_numbers == (2, 3)
    assert "def helper():" in modified_file.hunks_text


def test_parse_diff_rename_sets_source_path():
    parsed = parse_diff(RENAME_ONLY_DIFF)

    (renamed_file,) = parsed.files
    assert renamed_file.path == "src/renamed.py"
    assert renamed_file.source_path == "src/old_name.py"
    assert renamed_file.is_renamed is True
    assert renamed_file.added_line_numbers == ()


def test_parse_diff_delete():
    parsed = parse_diff(DELETE_DIFF)

    (deleted_file,) = parsed.files
    assert deleted_file.path == "src/new_file.py"
    assert deleted_file.is_removed is True
    assert deleted_file.removed_line_numbers == (1, 2)
    assert deleted_file.added_line_numbers == ()


def test_parse_diff_raises_on_malformed_hunk():
    # Plain non-diff text does NOT raise — unidiff treats it as an empty
    # patch (real diffs can carry leading commit-message text before the
    # actual diff content). A hunk header lying about its own line count
    # is what genuinely fails to parse.
    malformed = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,5 +1,5 @@\n"
        "+only one line but header claims 5\n"
    )

    with pytest.raises(UnidiffParseError):
        parse_diff(malformed)


def test_parse_diff_warns_and_truncates_on_file_count_cap(monkeypatch):
    import codesentinel.core.diff as diff_module

    monkeypatch.setattr(diff_module, "MAX_FILES", 1)

    parsed = parse_diff(MODIFY_AND_ADD_DIFF)

    assert len(parsed.files) == 1
    assert any("over the 1-file cap" in w for w in parsed.warnings)


def test_parse_diff_warns_on_size_cap_without_dropping_files(monkeypatch):
    import codesentinel.core.diff as diff_module

    monkeypatch.setattr(diff_module, "MAX_DIFF_BYTES", 10)

    parsed = parse_diff(MODIFY_AND_ADD_DIFF)

    assert len(parsed.files) == 2  # size cap only warns, doesn't truncate files
    assert any("byte review cap" in w for w in parsed.warnings)


def test_parse_diff_empty_input_returns_no_files():
    parsed = parse_diff("")

    assert parsed.files == ()
    assert parsed.warnings == ()

"""Release compatibility is reviewed metadata, never migration execution."""

from pathlib import Path

import pytest

from assistant.db.migration_compatibility import LEGACY_COMPATIBILITY, classify, load_graph

ROOT = Path(__file__).resolve().parents[2]


def revision(path: Path, name: str, parent: str | None, marker: str = "expand") -> None:
    (path / f"{name}.py").write_text(
        f"revision = {name!r}\ndown_revision = {parent!r}\nCOMPATIBILITY = {marker!r}\n",
        encoding="utf-8",
    )


def test_every_production_migration_is_classified_exactly_once() -> None:
    # load_graph rejects absent classifications and dual in-file/registry entries.
    graph = load_graph(ROOT / "migrations/versions")
    assert len(graph) == len(list((ROOT / "migrations/versions").glob("*.py")))
    assert LEGACY_COMPATIBILITY.keys() <= graph.keys()
    assert graph["0003"].compatibility == "breaking"
    assert graph["0005"].compatibility == "breaking"
    assert graph["0009"].compatibility == "breaking"
    assert graph["0015"].compatibility == "breaking"
    assert LEGACY_COMPATIBILITY["0017"] == "expand"
    assert graph["0017"].compatibility == "expand"
    assert classify(graph, "0016", "0017") == "expand"
    assert "0018" not in LEGACY_COMPATIBILITY
    assert graph["0018"].parents == ("0017",)
    assert graph["0018"].compatibility == "breaking"
    assert classify(graph, "0017", "0018") == "breaking"
    assert "0019" not in LEGACY_COMPATIBILITY
    assert graph["0019"].parents == ("0018",)
    assert graph["0019"].compatibility == "breaking"
    assert classify(graph, "0018", "0019") == "breaking"
    assert classify(graph, "0017", "0019") == "breaking"
    assert classify(graph, "0016", "0019") == "breaking"
    assert "0020" not in LEGACY_COMPATIBILITY
    assert graph["0020"].parents == ("0019",)
    assert graph["0020"].compatibility == "expand"
    assert graph["0021"].parents == ("0020",)
    assert graph["0021"].compatibility == "expand"
    assert classify(graph, "0019", "0020") == "expand"
    assert classify(graph, "0018", "0020") == "breaking"
    assert classify(graph, "0017", "0020") == "breaking"
    assert classify(graph, "0016", "0020") == "breaking"


def test_legacy_registry_classifies_immutable_migration(tmp_path: Path) -> None:
    (tmp_path / "0001.py").write_text("revision = '0001'\ndown_revision = None\n", encoding="utf-8")
    assert load_graph(tmp_path)["0001"].compatibility == "expand"


def test_in_file_marker_and_legacy_registry_cannot_both_classify_revision(tmp_path: Path) -> None:
    revision(tmp_path, "0001", None)
    with pytest.raises(ValueError, match="both in-file and in legacy registry"):
        load_graph(tmp_path)


def test_new_revision_uses_its_in_file_marker(tmp_path: Path) -> None:
    revision(tmp_path, "0018", None, "breaking")
    assert load_graph(tmp_path)["0018"].compatibility == "breaking"


def test_new_revision_without_marker_is_refused(tmp_path: Path) -> None:
    (tmp_path / "0018.py").write_text("revision = '0018'\ndown_revision = None\n", encoding="utf-8")
    with pytest.raises(ValueError, match="every migration must have"):
        load_graph(tmp_path)


def test_expand_range_excludes_the_already_applied_breaking_revision(tmp_path: Path) -> None:
    revision(tmp_path, "a", None, "breaking")
    revision(tmp_path, "b", "a")
    revision(tmp_path, "c", "b")
    graph = load_graph(tmp_path)
    assert classify(graph, "a", "c") == "expand"
    assert classify(graph, "b", "b") == "expand"
    assert classify(graph, "base", "c") == "breaking"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("c", "a", "migration path is not a forward upgrade"),
        ("missing", "c", "unknown migration revision"),
        ("a", "missing", "unknown migration revision"),
    ],
)
def test_unknown_or_reverse_paths_refuse(tmp_path: Path, old: str, new: str, message: str) -> None:
    revision(tmp_path, "a", None)
    revision(tmp_path, "b", "a")
    revision(tmp_path, "c", "b")
    with pytest.raises(ValueError, match=f"^{message}$"):
        classify(load_graph(tmp_path), old, new)


@pytest.mark.parametrize("marker", ["unknown", "", "EXPAND"])
def test_invalid_markers_refuse(tmp_path: Path, marker: str) -> None:
    revision(tmp_path, "a", None, marker)
    with pytest.raises(
        ValueError, match=r"^every migration must have expand or breaking compatibility$"
    ):
        load_graph(tmp_path)


def test_missing_marker_and_cycles_refuse(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("revision = 'a'\ndown_revision = None\n", encoding="utf-8")
    with pytest.raises(
        ValueError, match=r"^every migration must have expand or breaking compatibility$"
    ):
        load_graph(tmp_path)
    revision(tmp_path, "a", "b")
    revision(tmp_path, "b", "a")
    with pytest.raises(ValueError, match=r"^cyclic migration graph$"):
        classify(load_graph(tmp_path), "base", "b")


def test_merge_checks_every_new_branch(tmp_path: Path) -> None:
    revision(tmp_path, "a", None)
    revision(tmp_path, "b", "a")
    revision(tmp_path, "c", "a", "breaking")
    (tmp_path / "d.py").write_text(
        "revision = 'd'\ndown_revision = ('b', 'c')\nCOMPATIBILITY = 'expand'\n",
        encoding="utf-8",
    )
    graph = load_graph(tmp_path)
    assert classify(graph, "a", "d") == "breaking"
    assert classify(graph, "b,c", "d") == "expand"

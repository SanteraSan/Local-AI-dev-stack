from pathlib import Path

import pytest

from devmem.importer import collect_files, content_hash, prepare_file, split_markdown
from devmem.secrets_filter import SecretRejected


def test_split_on_atx_headings():
    text = "# Alpha\n\nBody A mentions phase3alpha.\n\n## Beta\n\nBody B.\n"
    sections = split_markdown(text)
    assert [section.heading for section in sections] == ["Alpha", "Beta"]
    assert sections[0].content.startswith("# Alpha")
    assert "phase3alpha" in sections[0].content
    assert sections[1].content.startswith("## Beta")


def test_preamble_and_no_headings_and_empty():
    with_preamble = split_markdown("intro line\n\n# Title\n\nbody\n")
    assert with_preamble[0].heading == ""
    assert with_preamble[0].content == "intro line"
    assert with_preamble[1].heading == "Title"

    whole = split_markdown("just a synthetic note")
    assert len(whole) == 1
    assert whole[0].content == "just a synthetic note"

    assert split_markdown("  \n\n") == []


def test_crlf_is_normalized():
    sections = split_markdown("# Title\r\n\r\nline\r\n")
    assert sections[0].heading == "Title"
    assert "\r" not in sections[0].content
    assert "line" in sections[0].content


def test_reimport_hash_is_stable(tmp_path: Path):
    path = tmp_path / "synthetic.md"
    path.write_text("# Note\n\nSynthetic body for the importer.\n", encoding="utf-8")
    first = prepare_file(path)
    second = prepare_file(path)
    assert first[0].content_hash == second[0].content_hash
    assert first[0].content_hash == content_hash(first[0].source_path, first[0].content)


def test_equivalent_paths_share_a_relative_source(tmp_path: Path):
    root = tmp_path / "checkout"
    folder = root / "research"
    folder.mkdir(parents=True)
    target = folder / "notes.md"
    target.write_text("# Note\n\nSynthetic body for the importer.\n", encoding="utf-8")

    via_relative = prepare_file(Path("research/notes.md"), root)
    via_absolute = prepare_file(target.resolve(), root)
    via_dotdot = prepare_file(folder / ".." / "research" / "notes.md", root)

    assert via_relative[0].source_path == "research/notes.md"
    assert via_absolute[0].source_path == "research/notes.md"
    assert via_dotdot[0].source_path == "research/notes.md"
    assert via_relative[0].content_hash == via_absolute[0].content_hash == via_dotdot[0].content_hash

    outside = tmp_path / "outside.md"
    outside.write_text("# Out\n\nNot inside the checkout.\n", encoding="utf-8")
    prepared = prepare_file(outside, root)
    assert prepared[0].source_path == str(outside.resolve())


def test_prepare_rejects_a_secret_without_touching_storage(tmp_path: Path):
    secret = "sk-" + ("a" * 40)
    path = tmp_path / "synthetic.md"
    path.write_text("# Title\n\nsee " + secret + "\n", encoding="utf-8")
    with pytest.raises(SecretRejected) as caught:
        prepare_file(path)
    assert caught.value.pattern_type == "openai_key"
    assert secret not in str(caught.value)


def test_collect_files_path_and_glob(tmp_path: Path):
    (tmp_path / "one.md").write_text("# One\n\nbody\n", encoding="utf-8")
    nested = tmp_path / "notes"
    nested.mkdir()
    (nested / "two.md").write_text("# Two\n\nbody\n", encoding="utf-8")

    assert collect_files(tmp_path, "one.md", None) == [(tmp_path / "one.md").resolve()]
    found = collect_files(tmp_path, None, "notes/*.md")
    assert found == [(nested / "two.md").resolve()]
    with pytest.raises(ValueError):
        collect_files(tmp_path, None, None)
    with pytest.raises(ValueError):
        collect_files(tmp_path, "one.md", "*.md")

import pytest

from app.utils.file_parser import split_text_into_chunks


def test_splitting_terminates_with_large_overlap():
    # overlap > 0.3 * chunk_size used to make the chunker never advance
    text = ("a" * 60 + "。" + "b" * 200) * 4

    chunks = split_text_into_chunks(text, chunk_size=100, overlap=80)

    assert chunks
    assert all(chunk for chunk in chunks)
    assert len(chunks) < len(text)


def test_splitting_still_splits_on_sentence_boundaries():
    text = "句子内容。" * 200

    chunks = split_text_into_chunks(text, chunk_size=100, overlap=20)

    assert chunks
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert chunks[0].endswith("。")


def test_overlap_must_be_smaller_than_chunk_size():
    with pytest.raises(ValueError):
        split_text_into_chunks("x" * 500, chunk_size=100, overlap=100)
    with pytest.raises(ValueError):
        split_text_into_chunks("x" * 500, chunk_size=100, overlap=150)
    with pytest.raises(ValueError):
        split_text_into_chunks("x" * 500, chunk_size=100, overlap=-1)
    with pytest.raises(ValueError):
        split_text_into_chunks("x" * 500, chunk_size=0, overlap=0)

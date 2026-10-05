"""What a person may paste to name a Drive folder: its id, or a Drive folder link. Parsed,
never fetched; anything else is refused before any call to Google."""

import pytest

from docforge.sync.folders import InvalidFolder, parse_folder

ID = "1AbC-dEf_GhIjKlMnOpQrStUvWxYz0123"


@pytest.mark.parametrize(
    "reference",
    [
        ID,
        f"  {ID}  ",
        f"https://drive.google.com/drive/folders/{ID}",
        f"https://drive.google.com/drive/folders/{ID}?usp=sharing",
        f"https://drive.google.com/drive/u/1/folders/{ID}",
        f"https://drive.google.com/open?id={ID}",
        f"drive.google.com/drive/folders/{ID}",
    ],
)
def test_an_id_or_a_drive_folder_link_gives_the_folder_id(reference: str) -> None:
    assert parse_folder(reference) == ID


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "short",
        f"{ID}' or name contains '",  # would break out of a Drive query
        f"https://evil.example/drive/folders/{ID}",
        f"https://drive.google.com.evil.example/drive/folders/{ID}",
        f"https://docs.google.com/document/d/{ID}/edit",  # a document, not a folder
        f"http://drive.google.com/drive/folders/{ID}",  # not https
        "x" * 200,
    ],
)
def test_anything_else_is_refused(reference: str) -> None:
    with pytest.raises(InvalidFolder):
        parse_folder(reference)

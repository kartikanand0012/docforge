"""What a sync needs from Google Drive, whatever client provides it."""

from dataclasses import dataclass
from typing import Protocol

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDES = "application/vnd.google-apps.presentation"
VERIFY_PREFIX = "docforge-verify-"

# Google's own files have no bytes: they are exported to a format the converter reads.
EXPORTS: dict[str, str] = {
    GOOGLE_DOC: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    GOOGLE_SHEET: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    GOOGLE_SLIDES: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
# Files downloaded as they are (the formats DocForge accepts; it checks the bytes again).
DOWNLOADS = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/png",
        "image/jpeg",
        "image/tiff",
    }
)


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    md5: str | None  # binary files only
    version: str | None  # changes with every edit; how Google's own files are compared
    size: int | None
    parent: str | None = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER

    @property
    def is_google(self) -> bool:
        return self.mime_type in EXPORTS


class DriveError(Exception):
    """Drive could not be asked."""


class DriveNotFound(DriveError):
    """The file or folder does not exist, or is not shared with DocForge (Drive says the same)."""


class DriveRateLimited(DriveError):
    """Drive's quota is used up for now: try again later."""


class DriveUnavailable(DriveError):
    """Drive failed or could not be reached: try again later."""


class FileTooLarge(DriveError):
    """The file is larger than DocForge takes."""


class DriveClient(Protocol):
    service_account_email: str

    def get(self, file_id: str) -> DriveFile: ...

    def list_children(
        self, folder_id: str, page_token: str | None = None
    ) -> tuple[list[DriveFile], str | None]:
        """One page of a folder's children, and the token for the next (None at the end)."""
        ...

    def download(self, file_id: str, max_bytes: int) -> bytes: ...

    def export(self, file_id: str, mime_type: str, max_bytes: int) -> bytes: ...

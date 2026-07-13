import pathlib
from pypdf import PdfReader


_DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"


def load_resume_text(path: str) -> str:
    file_path = pathlib.Path(path).resolve()

    if not str(file_path).startswith(str(_DATA_DIR)):
        raise ValueError(f"Resume path must be under {_DATA_DIR}")

    if file_path.suffix.lower() != ".pdf":
        raise ValueError("Resume file must be a PDF")

    if not file_path.exists():
        raise FileNotFoundError(f"Resume file not found: {file_path}")

    reader = PdfReader(file_path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)

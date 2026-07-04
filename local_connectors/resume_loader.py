from pypdf import PdfReader

def load_resume_text(path: str) -> str:
    reader = PdfReader(path)

    return "\n".join(page.extract_text() or "" for page in reader.pages)

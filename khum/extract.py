"""올린 글 → 문단 목록.

지원: 붙여넣기 · .txt · .md · .docx · .pdf   (한글 .hwp/.hwpx 는 '다른 이름으로 저장 → .docx' 후 올린다)
문단 = 빈 줄로 나뉜 덩어리(.txt/.md/.pdf) 또는 워드 문단(.docx). 제목·목록도 한 문단으로 둔다.
"""
import re
from pathlib import Path

SUPPORTED = (".txt", ".md", ".docx", ".pdf")


def _decode(data):
    for enc in ("utf-8-sig", "cp949", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def split_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text)]
    return [p for p in paras if p]


def _pdf(path):
    """PDF 글상자(block) 하나 = 문단 하나. 블록 안 줄바꿈은 이어 붙이고 쪽 번호는 버린다."""
    import fitz
    paras = []
    with fitz.open(path) as pdf:
        for page in pdf:
            for block in page.get_text("blocks"):
                if block[6] != 0:  # 그림 블록
                    continue
                lines = [l.strip() for l in block[4].splitlines() if l.strip()]
                if not lines or re.fullmatch(r"[-–—\s]*\d+[-–—\s]*", " ".join(lines)):
                    continue
                paras.append(_join(lines))
    return paras


def _join(lines):
    """줄 끝 이음: 한글 뒤는 띄어 붙이고, 하이픈으로 끊긴 영어는 붙인다."""
    out = ""
    for l in lines:
        if not out:
            out = l
        elif out.endswith("-") and re.match(r"[a-z]", l):
            out = out[:-1] + l
        else:
            out += " " + l
    return out


def _docx(path):
    import docx
    d = docx.Document(path)
    return [p.text.strip() for p in d.paragraphs if p.text.strip()]


def paragraphs(path):
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".txt", ".md"):
        return split_text(_decode(path.read_bytes()))
    if ext == ".docx":
        return _docx(path)
    if ext == ".pdf":
        return _pdf(path)
    if ext in (".hwp", ".hwpx"):
        raise ValueError("한글 파일은 한글에서 '다른 이름으로 저장 → Word 문서(.docx)'로 바꾼 뒤 올려 주세요.")
    raise ValueError(f"이 형식은 아직 못 읽습니다: {ext} (txt · md · docx · pdf 또는 붙여넣기)")


if __name__ == "__main__":
    import sys
    for i, p in enumerate(paragraphs(sys.argv[1]), 1):
        print(f"[{i}] {p[:120]}")

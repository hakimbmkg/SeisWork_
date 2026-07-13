"""Presentation builder helpers - SeisWork GUI by HakimBMKG."""
from pathlib import Path
import json

_DEFAULT = {
    "title": "SeisWork Presentation",
    "author": "HakimBMKG",
    "theme": "dark",
    "slides": [],
}


def load_presentation(cfg_dir: Path) -> dict:
    f = cfg_dir / "presentation.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return dict(_DEFAULT)


def save_presentation(cfg_dir: Path, data: dict) -> None:
    out = cfg_dir / "presentation.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

"""Global template assets must be deliverable by the explicit static router."""

import ast
import re
from pathlib import Path


def test_base_assets_have_registered_mime_and_existing_files():
    root = Path(__file__).resolve().parents[2] / "src/omakase/plus"
    tree = ast.parse((root / "routes.py").read_text(encoding="utf-8"))
    assets = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_STATIC_ASSETS"
            for target in node.targets
        )
    )
    linked = re.findall(
        r'(?:href|src)="/plus/static/([^?"/]+)',
        (root / "templates/base.html").read_text(encoding="utf-8"),
    )
    assert linked
    for name in linked:
        assert name in assets, f"Global asset {name} would return JSON404 instead of its MIME"
        assert (root / "static" / name).is_file()
        if name.endswith(".css"):
            assert assets[name] == "text/css"
        elif name.endswith(".js"):
            assert assets[name] == "application/javascript"

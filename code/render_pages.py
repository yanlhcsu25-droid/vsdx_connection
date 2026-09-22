"""Render VSDX pages to pageN.png using an explicitly configured renderer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _page_count(vsdx: Path) -> int:
    import re
    import zipfile
    with zipfile.ZipFile(vsdx) as archive:
        pages = [
            int(match.group(1))
            for name in archive.namelist()
            if (match := re.fullmatch(r"visio/pages/page(\d+)\.xml", name))
        ]
    return max(pages, default=1)


def _pngs(output_dir: Path) -> list[Path]:
    return sorted(output_dir.glob("page*.png"))


def _run_command(command: list[str], *, timeout: int, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True, timeout=timeout,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _normalize_single_png(output_dir: Path, vsdx: Path) -> None:
    pngs = _pngs(output_dir)
    if len(pngs) == 1 and pngs[0].name != "page1.png":
        pngs[0].replace(output_dir / "page1.png")


def render_vsdx_pages(
    vsdx: Path,
    output_dir: Path,
    *,
    config_path: Path | None = None,
    timeout: int = 180,
) -> Path:
    """Render all VSDX pages and return the directory containing pageN.png.

    A renderer command may be supplied in JSON as ``{"command": [...],
    "timeout": 180}``, or through ``VSDX_RENDER_COMMAND`` as a JSON array.
    Command elements may use ``{vsdx}`` and ``{output_dir}`` placeholders.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    page_count = _page_count(vsdx)
    configured = None
    if config_path is not None:
        configured = json.loads(config_path.read_text(encoding="utf-8"))
    elif os.environ.get("VSDX_RENDER_COMMAND"):
        configured = {
            "command": json.loads(os.environ["VSDX_RENDER_COMMAND"]),
            "timeout": timeout,
        }
    attempts: list[tuple[str, list[str]]] = []
    if configured:
        command = configured.get("command")
        if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
            raise ValueError("render config command must be a JSON string array")
        attempts.append(("configured renderer", [
            item.format(vsdx=str(vsdx.resolve()), output_dir=str(output_dir.resolve()))
            for item in command
        ]))
    drawio = shutil.which("drawio")
    if drawio:
        attempts.append(("drawio", [drawio, "--headless", "--export", "--format", "png",
                                     "--output", str(output_dir), str(vsdx.resolve())]))
    soffice = shutil.which("soffice")
    pdftoppm = shutil.which("pdftoppm")
    if soffice and pdftoppm:
        # The PDF bridge is handled below because it needs a temporary PDF directory.
        attempts.append(("soffice-pdf", []))

    failures = []
    for name, command in attempts:
        try:
            if name == "soffice-pdf":
                with tempfile.TemporaryDirectory(prefix="vsdx-render-") as temp:
                    temp_dir = Path(temp)
                    subprocess.run(
                        [soffice, "--headless", "--convert-to", "pdf",
                         "--outdir", str(temp_dir), str(vsdx.resolve())],
                        check=True, timeout=timeout,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    pdfs = list(temp_dir.glob("*.pdf"))
                    if len(pdfs) != 1:
                        raise RuntimeError("soffice did not produce exactly one PDF")
                    subprocess.run(
                        [pdftoppm, "-png", "-r", "150", str(pdfs[0]),
                         str(output_dir / "page")],
                        check=True, timeout=timeout,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    for rendered in sorted(output_dir.glob("page-*.png")):
                        number = rendered.stem.rsplit("-", 1)[-1]
                        rendered.replace(output_dir / f"page{number}.png")
            else:
                _run_command(command, timeout=timeout, output_dir=output_dir)
                _normalize_single_png(output_dir, vsdx)
            pngs = _pngs(output_dir)
            if len(pngs) >= page_count and all((output_dir / f"page{i}.png").exists()
                                               for i in range(1, page_count + 1)):
                return output_dir
            raise RuntimeError(f"renderer produced {len(pngs)} PNG file(s), expected {page_count}")
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            failures.append(f"{name}: {error}")
            for stale in _pngs(output_dir):
                stale.unlink()

    detail = "\n".join(failures) if failures else "no renderer was found"
    raise RuntimeError(
        "Unable to render VSDX pages automatically. Configure a renderer with "
        "--render-config or VSDX_RENDER_COMMAND (JSON string array). Attempts:\n" + detail
    )

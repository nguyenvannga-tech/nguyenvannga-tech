#!/usr/bin/env python3
"""Generate split code statistics for a GitHub profile README.

Project layout:
- app/      -> Frontend (Expo / React Native)
- backend/  -> Backend (NestJS / Rust / AI)
- selected backend paths/files -> Data & Infrastructure

Only the content between CODE_STATS_START and CODE_STATS_END is replaced.
All other README content and styling remain unchanged.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

START_MARKER = "<!-- CODE_STATS_START -->"
END_MARKER = "<!-- CODE_STATS_END -->"

BAR_LENGTH = 36
TOP_LANGUAGES = 5
DETAIL_LANGUAGES = 12

COMMON_EXCLUDE_DIRS = {
    ".git", "node_modules", "vendor", "dist", "build", ".next", "target",
    "coverage", ".venv", "venv", "__pycache__", ".expo", ".turbo", ".cache",
}

FRONTEND_EXCLUDE_DIRS = COMMON_EXCLUDE_DIRS | {"android", "ios", "assets", "public"}
BACKEND_EXCLUDE_DIRS = COMMON_EXCLUDE_DIRS | {"redis-certs"}
BACKEND_CATEGORY_EXCLUDE_DIRS = BACKEND_EXCLUDE_DIRS | {
    "prisma", ".sqlx", "deploy", "grafana", "prometheus",
    "loki", "promtail", "njs",
}

EXCLUDED_LANGUAGES = {"Markdown", "JSON", "YAML", "XML", "TOML", "Text", "CSV", "SVG"}

DATA_INFRA_DIRS = (
    "prisma", ".sqlx", "deploy", "grafana", "prometheus",
    "loki", "promtail", "njs",
)

DATA_INFRA_FILES = (
    "openapi.json", "nginx.conf", "docker-compose.yml",
    "docker-compose.observability.yml", "kafka_topics.json",
    "debezium-outbox-connector.json", "Dockerfile", "Dockerfile.rust",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--readme", required=True, type=Path)
    parser.add_argument("--repository-count", type=int, default=1)
    parser.add_argument("--frontend-dir", default="app")
    parser.add_argument("--backend-dir", default="backend")
    return parser.parse_args()


def format_number(value: int) -> str:
    return f"{value:,}"


def percentage(part: int, whole: int) -> float:
    return part / whole * 100.0 if whole else 0.0


def empty_cloc() -> dict:
    return {"header": {"n_files": 0}, "SUM": {"blank": 0, "comment": 0, "code": 0}}


def run_cloc(paths: list[Path], output: Path, exclude_dirs: set[str] | None = None) -> None:
    existing_paths = [path for path in paths if path.exists()]
    if not existing_paths:
        output.write_text(json.dumps(empty_cloc(), indent=2), encoding="utf-8")
        return

    command = [
        "cloc", *[str(path) for path in existing_paths],
        "--json", f"--out={output}",
        "--force-lang=Dockerfile,Dockerfile",
        "--exclude-lang=" + ",".join(sorted(EXCLUDED_LANGUAGES)),
    ]

    if exclude_dirs:
        command.append("--exclude-dir=" + ",".join(sorted(exclude_dirs)))

    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            "cloc failed.\n"
            f"Command: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def read_cloc(path: Path) -> tuple[list[dict[str, int | str]], dict[str, int]]:
    with path.open("r", encoding="utf-8") as file:
        raw = json.load(file)

    languages: list[dict[str, int | str]] = []

    for language, values in raw.items():
        if language in {"header", "SUM"} or language in EXCLUDED_LANGUAGES:
            continue
        if not isinstance(values, dict) or "code" not in values:
            continue

        code = int(values.get("code", 0))
        if code <= 0:
            continue

        languages.append({
            "language": language,
            "files": int(values.get("nFiles", 0)),
            "blank": int(values.get("blank", 0)),
            "comment": int(values.get("comment", 0)),
            "code": code,
        })

    languages.sort(key=lambda item: int(item["code"]), reverse=True)

    totals = {
        "files": sum(int(item["files"]) for item in languages),
        "blank": sum(int(item["blank"]) for item in languages),
        "comment": sum(int(item["comment"]) for item in languages),
        "code": sum(int(item["code"]) for item in languages),
    }
    return languages, totals


def merge_language_stats(groups: list[list[dict[str, int | str]]]) -> tuple[list[dict[str, int | str]], dict[str, int]]:
    merged: dict[str, dict[str, int | str]] = {}

    for group in groups:
        for item in group:
            name = str(item["language"])
            merged.setdefault(name, {
                "language": name, "files": 0, "blank": 0, "comment": 0, "code": 0
            })
            for key in ("files", "blank", "comment", "code"):
                merged[name][key] = int(merged[name][key]) + int(item[key])

    languages = sorted(merged.values(), key=lambda item: int(item["code"]), reverse=True)
    totals = {
        "files": sum(int(item["files"]) for item in languages),
        "blank": sum(int(item["blank"]) for item in languages),
        "comment": sum(int(item["comment"]) for item in languages),
        "code": sum(int(item["code"]) for item in languages),
    }
    return languages, totals


def build_bar(value: float) -> str:
    filled = round(value / 100.0 * BAR_LENGTH)
    if value > 0 and filled == 0:
        filled = 1
    filled = max(0, min(BAR_LENGTH, filled))
    return "█" * filled + "░" * (BAR_LENGTH - filled)


def category_card(title: str, path_label: str, totals: dict[str, int], total_code: int) -> list[str]:
    share = percentage(totals["code"], total_code)
    return [
        '<td width="33%" align="center" valign="top">',
        f"<strong>{html.escape(title)}</strong>",
        "<br/>",
        f"<sub><code>{html.escape(path_label)}</code></sub>",
        "<br/><br/>",
        f"<strong>{format_number(totals['code'])}</strong><br/>",
        "<sub>Code lines</sub>",
        "<br/>",
        f"<strong>{format_number(totals['files'])}</strong><br/>",
        "<sub>Source files</sub>",
        "<br/>",
        f"<strong>{share:.1f}%</strong><br/>",
        "<sub>Share of total</sub>",
        "</td>",
    ]


def build_section(
    all_languages: list[dict[str, int | str]],
    total: dict[str, int],
    frontend: dict[str, int],
    backend: dict[str, int],
    data_infra: dict[str, int],
    repository_count: int,
) -> str:
    timezone_name = os.getenv("PROFILE_TIMEZONE", "UTC")
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = ZoneInfo("UTC")
        timezone_name = "UTC"

    updated_at = datetime.now(timezone).strftime("%d/%m/%Y %H:%M")
    total_code = total["code"] or 1

    lines = [
        START_MARKER,
        "",
        '<div align="center">',
        "",
        "<table>",
        "<tr>",
        f'<td align="center" width="20%"><strong>{format_number(total["code"])}</strong><br/><sub>Code lines</sub></td>',
        f'<td align="center" width="20%"><strong>{format_number(total["comment"])}</strong><br/><sub>Comments</sub></td>',
        f'<td align="center" width="20%"><strong>{format_number(total["files"])}</strong><br/><sub>Source files</sub></td>',
        f'<td align="center" width="20%"><strong>{repository_count}</strong><br/><sub>Repository</sub></td>',
        f'<td align="center" width="20%"><strong>{len(all_languages)}</strong><br/><sub>Languages</sub></td>',
        "</tr>",
        "</table>",
        "",
        "<br/>",
        "",
        "<table>",
        "<tr>",
    ]

    lines.extend(category_card("Frontend", "app/", frontend, total_code))
    lines.extend(category_card("Backend", "backend/", backend, total_code))
    lines.extend(category_card(
        "Data & Infrastructure",
        "backend/prisma · deploy · observability",
        data_infra,
        total_code,
    ))

    lines.extend(["</tr>", "</table>", "", "<br/>", "", "<table>"])

    for item in all_languages[:TOP_LANGUAGES]:
        name = html.escape(str(item["language"]))
        code = int(item["code"])
        share = percentage(code, total_code)
        lines.extend([
            "<tr>",
            f'<td width="130" align="left"><strong>{name}</strong></td>',
            f'<td align="left">{build_bar(share)}</td>',
            f'<td width="60" align="right"><strong>{share:.1f}%</strong></td>',
            "</tr>",
        ])

    lines.extend([
        "</table>",
        "",
        "</div>",
        "",
        "<details>",
        "<summary><strong>View detailed language statistics</strong></summary>",
        "",
        "<br/>",
        "",
        "<table>",
        "<thead>",
        "<tr>",
        '<th align="left">Language</th>',
        '<th align="right">Files</th>',
        '<th align="right">Code lines</th>',
        '<th align="right">Share</th>',
        "</tr>",
        "</thead>",
        "<tbody>",
    ])

    for item in all_languages[:DETAIL_LANGUAGES]:
        name = html.escape(str(item["language"]))
        files = int(item["files"])
        code = int(item["code"])
        share = percentage(code, total_code)
        lines.append(
            f'<tr><td align="left">{name}</td>'
            f'<td align="right">{format_number(files)}</td>'
            f'<td align="right">{format_number(code)}</td>'
            f'<td align="right">{share:.1f}%</td></tr>'
        )

    lines.extend([
        "</tbody>",
        "</table>",
        "",
        "</details>",
        "",
        '<div align="center">',
        "",
        "<sub>",
        f"Updated {updated_at} &nbsp;&nbsp; "
        f'Generated with <code>cloc</code> &nbsp;&nbsp; '
        f"Authored source only",
        "</sub>",
        "",
        "</div>",
        "",
        END_MARKER,
    ])

    return "\n".join(lines)


def replace_section(readme: str, new_section: str) -> str:
    start = readme.find(START_MARKER)
    end = readme.find(END_MARKER)

    if start == -1 or end == -1 or end < start:
        raise ValueError(
            f"README must contain both {START_MARKER!r} and {END_MARKER!r}."
        )

    end += len(END_MARKER)
    return readme[:start] + new_section + readme[end:]


def main() -> None:
    args = parse_args()

    if shutil.which("cloc") is None:
        raise RuntimeError("cloc is not installed. On macOS run: brew install cloc")

    repository = args.repository.resolve()
    readme = args.readme.resolve()
    frontend_root = repository / args.frontend_dir
    backend_root = repository / args.backend_dir

    if not repository.is_dir():
        raise FileNotFoundError(f"Repository directory not found: {repository}")
    if not frontend_root.is_dir():
        raise FileNotFoundError(f"Frontend directory not found: {frontend_root}")
    if not backend_root.is_dir():
        raise FileNotFoundError(f"Backend directory not found: {backend_root}")
    if not readme.is_file():
        raise FileNotFoundError(f"README not found: {readme}")

    with tempfile.TemporaryDirectory(prefix="profile-code-stats-") as temp_dir:
        temp = Path(temp_dir)
        frontend_json = temp / "frontend.json"
        backend_json = temp / "backend.json"
        data_json = temp / "data.json"

        run_cloc([frontend_root], frontend_json, FRONTEND_EXCLUDE_DIRS)
        run_cloc([backend_root], backend_json, BACKEND_CATEGORY_EXCLUDE_DIRS)

        data_paths = [backend_root / name for name in DATA_INFRA_DIRS]
        data_paths.extend(backend_root / name for name in DATA_INFRA_FILES)
        run_cloc(data_paths, data_json, BACKEND_EXCLUDE_DIRS)

        frontend_languages, frontend_totals = read_cloc(frontend_json)
        backend_languages, backend_totals = read_cloc(backend_json)
        data_languages, data_totals = read_cloc(data_json)

    all_languages, total = merge_language_stats(
        [frontend_languages, backend_languages, data_languages]
    )

    new_section = build_section(
        all_languages,
        total,
        frontend_totals,
        backend_totals,
        data_totals,
        args.repository_count,
    )

    original = readme.read_text(encoding="utf-8")
    readme.write_text(replace_section(original, new_section), encoding="utf-8")

    print("README statistics updated successfully.")
    print(f"Frontend: {format_number(frontend_totals['code'])} lines")
    print(f"Backend: {format_number(backend_totals['code'])} lines")
    print(f"Data & Infrastructure: {format_number(data_totals['code'])} lines")
    print(f"Total authored source: {format_number(total['code'])} lines")


if __name__ == "__main__":
    main()
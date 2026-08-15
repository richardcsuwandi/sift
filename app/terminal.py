"""Rich rendering and review helpers for Sift's terminal interface."""

from __future__ import annotations

import json
import time
import unicodedata
from pathlib import Path

from rich import box
from rich.console import Group
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from app import organizer
from app.plan import save_plan


BLUE = "#1180f5"
PIXEL_WORDMARK = """[bold #1180f5]█▀▀  █  █▀▀  ▀█▀[/]
[bold #1180f5]▀▀█  █  █▀    █ [/]
[bold #1180f5]▀▀▀  ▀  ▀     ▀ [/]"""


def clean_snippet(value: object, *, max_lines: int = 3, max_chars: int = 320) -> str:
    """Make extracted evidence readable without changing the searchable index."""
    value = unicodedata.normalize(
        "NFKC", str(value).replace("\x02", "").replace("\x03", "")
    )
    cleaned = []
    for character in value:
        category = unicodedata.category(character)
        if character in "\n\t" or not category.startswith("C"):
            cleaned.append(character)
    lines = [" ".join(line.split()) for line in "".join(cleaned).splitlines()]
    text = "\n".join(line for line in lines if line)[:max_chars].rstrip()
    text = "\n".join(text.splitlines()[:max_lines])
    if len(text) == max_chars:
        text = text.rsplit(" ", 1)[0].rstrip() + "…"
    return text


def finder_url(path: str | Path) -> str:
    """URL for the containing directory, suitable for an OSC-8 terminal link."""
    return Path(path).expanduser().resolve().parent.as_uri()


def banner(console: Console, status: dict) -> None:
    age = "not indexed"
    if status.get("last_scanned"):
        seconds = max(0, int(time.time() - status["last_scanned"]))
        age = "just now" if seconds < 60 else f"{seconds // 60}m ago"
    details = Text()
    details.append("your local file copilot\n\n", style="bold")
    details.append("folder  ", style="dim")
    details.append(status["root"] + "\n")
    details.append("model   ", style="dim")
    details.append(status.get("default") or "not available")
    details.append("\nindex   ", style="dim")
    details.append(f"{status.get('file_count', 0)} files · {age}")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(width=19)
    grid.add_column()
    grid.add_row(Text.from_markup(PIXEL_WORDMARK), details)
    console.print(Panel(grid, border_style=BLUE, padding=(1, 2), expand=False))


def status_panel(console: Console, status: dict) -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column()
    table.add_row("Folder", status["root"])
    table.add_row("Index", f"{status['file_count']} files" if status["indexed"] else "not scanned")
    table.add_row("Chat", status.get("default") or "not available")
    table.add_row("Embedding", status.get("embed_model") or "off · keyword search only")
    table.add_row("Vision", status.get("vision_default") or "off · filenames only")
    installed = status.get("installed") or []
    table.add_row("Ollama", f"{len(installed)} model{'s' if len(installed) != 1 else ''} installed")
    console.print(Panel(table, title="Sift status", border_style=BLUE))


def models_panel(console: Console, value: dict, *, active: str | None = None) -> None:
    """Explain which installed model serves each Sift capability."""
    chat = value.get("chat_models") or []
    selected = active or value.get("default")
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", justify="right")
    table.add_column()
    table.add_column(style="dim")
    table.add_row("Chat", selected or "not available", "questions · plans · organization")
    table.add_row(
        "Embedding",
        value.get("embed_model") or "not installed",
        "semantic file search" if value.get("embed_model") else "keyword search fallback",
    )
    table.add_row(
        "Vision",
        value.get("vision_default") or "not installed",
        "image contents with /scan --images",
    )
    alternatives = [name for name in chat if name != selected]
    if alternatives:
        table.add_row("Other chat", ", ".join(alternatives), "select with /model NAME")
    console.print(Panel(table, title="Local model roles", border_style=BLUE))


def consume_events(console: Console, events, *, label: str) -> tuple[list[dict], str | None]:
    suggestions: list[dict] = []
    error = None
    with Progress(
        SpinnerColumn(style=BLUE),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style=BLUE, finished_style=BLUE),
        TextColumn("{task.completed:.0f}/{task.total:.0f}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(label, total=1)
        try:
            for event in events:
                kind = event.get("type")
                if kind == "begin":
                    progress.update(task, total=max(event.get("total", 0), 1), completed=0)
                elif kind == "planning":
                    progress.update(task, description="Planning a consistent folder structure…")
                elif kind == "interpreted":
                    progress.update(
                        task,
                        description=f"{event.get('action', 'Working').title()} · {event.get('matched', 0)} matches",
                        total=max(event.get("matched", 0), 1),
                    )
                elif kind == "reading":
                    name = str(event.get("filename", ""))
                    progress.update(
                        task,
                        description=f"Reading {name[:42]}",
                        total=max(event.get("total", 0), 1),
                        completed=max(event.get("i", 1) - 1, 0),
                    )
                elif kind == "item":
                    suggestions.append(event["suggestion"])
                    progress.update(task, completed=event.get("i", len(suggestions)))
                elif kind == "revised":
                    suggestions = event.get("suggestions") or []
                elif kind == "error":
                    error = event.get("detail") or "The operation failed."
                elif kind == "done":
                    progress.update(task, completed=progress.tasks[task].total)
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
    return suggestions, error


def proposed_tree(
    root: Path,
    suggestions: list[dict],
    *,
    categories: list[str] | None = None,
    folders: list[dict] | None = None,
    files_per_folder: int = 4,
) -> Tree:
    """A compact destination tree suitable for a live terminal viewport."""
    planned: set[tuple[str, ...]] = {
        (str(category),) for category in (categories or []) if str(category).strip()
    }
    for folder in folders or []:
        category = str(folder.get("category") or "").strip()
        name = str(folder.get("name") or "").strip()
        if category and name:
            planned.add((category, name))

    leaves: dict[tuple[str, ...], list[tuple[str, str | None]]] = {}
    for item in suggestions:
        target = Path(destination(item, root))
        parts = target.parts
        if not parts:
            continue
        folder_path, new_name = tuple(parts[:-1]), parts[-1]
        planned.add(folder_path)
        source_name = Path(str(item.get("path") or "")).name
        note = source_name if source_name and source_name != new_name else None
        leaves.setdefault(folder_path, []).append((new_name, note))

    # Every nested folder implies all its parents, including ones introduced by
    # an explicit natural-language destination rather than the vocabulary pass.
    paths = set(planned)
    for path in tuple(paths):
        for depth in range(1, len(path)):
            paths.add(path[:depth])

    children: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for path in paths:
        if path:
            children.setdefault(path[:-1], []).append(path)

    tree = Tree(f"[bold {BLUE}]{escape(root.name)}/[/]", guide_style="dim")

    def add(parent: Tree, path: tuple[str, ...]) -> None:
        for child_path in sorted(children.get(path, []), key=lambda value: value[-1].casefold()):
            child_files = sorted(leaves.get(child_path, []), key=lambda value: value[0].casefold())
            count = len(child_files)
            label = f"[bold {BLUE}]{escape(child_path[-1])}/[/]"
            if count:
                label += f" [dim]({count})[/]"
            branch = parent.add(label)
            add(branch, child_path)

        # Rename-only files can live at the root, so render leaves for any path,
        # not only paths represented by a directory branch above.
        root_files = sorted(leaves.get(path, []), key=lambda value: value[0].casefold())
        for name, old_name in root_files[:files_per_folder]:
            rename = f" [dim]← {escape(old_name)}[/]" if old_name else ""
            parent.add(f"[green]{escape(name)}[/]{rename}")
        hidden = len(root_files) - files_per_folder
        if hidden > 0:
            parent.add(f"[dim]… {hidden} more[/]")

    add(tree, ())
    return tree


def consume_organize_events(
    console: Console, events, *, root: Path
) -> tuple[list[dict], str | None]:
    """Consume organizer events while constructing its destination tree live."""
    suggestions: list[dict] = []
    categories: list[str] = []
    folders: list[dict] = []
    error = None
    total = 1
    progress = Progress(
        SpinnerColumn(style=BLUE),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style=BLUE, finished_style=BLUE),
        TextColumn("{task.completed:.0f}/{task.total:.0f}"),
        TimeElapsedColumn(),
        console=console,
    )
    task = progress.add_task("Planning folders…", total=total)

    def display():
        tree = proposed_tree(
            root, suggestions, categories=categories, folders=folders
        )
        panel = Panel(
            tree,
            title=f"Proposed tree · {len(suggestions)}/{total}",
            border_style=BLUE,
            padding=(0, 1),
        )
        return Group(progress, panel)

    with Live(display(), console=console, refresh_per_second=8, transient=False) as live:
        try:
            for event in events:
                kind = event.get("type")
                if kind in {"begin", "planning"}:
                    total = max(event.get("total", 0), 1)
                    description = (
                        "Planning folders from this folder's history…"
                        if event.get("learned")
                        else "Planning folders…"
                    )
                    progress.update(task, total=total, completed=0, description=description)
                elif kind == "vocabulary":
                    categories = event.get("categories") or []
                    folders = event.get("folders") or []
                    progress.update(task, description="Classifying files…")
                elif kind == "reading":
                    name = str(event.get("filename", ""))
                    progress.update(
                        task,
                        description=f"Reading {name[:42]}",
                        total=max(event.get("total", total), 1),
                        completed=max(event.get("i", 1) - 1, 0),
                    )
                elif kind == "item":
                    suggestions.append(event["suggestion"])
                    progress.update(task, completed=event.get("i", len(suggestions)))
                elif kind == "revised":
                    suggestions = event.get("suggestions") or []
                    total = max(len(suggestions), 1)
                    progress.update(
                        task, description="Tidying folders…", total=total,
                        completed=len(suggestions),
                    )
                elif kind == "error":
                    error = event.get("detail") or "The operation failed."
                elif kind == "done":
                    progress.update(task, description="Organization plan ready", completed=total)
                live.update(display())
                live.refresh()
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            live.update(display())
    return suggestions, error


def _action(item: dict) -> str:
    if item.get("trash"):
        return "TRASH"
    if item.get("folder_rename") or item.get("rename_only"):
        return "RENAME"
    return "MOVE"


def destination(item: dict, root: Path) -> str:
    src = Path(str(item["path"]))
    name = str(item.get("suggested_filename") or src.name)
    if item.get("trash"):
        return f"Trash/{name}"
    if item.get("folder_rename"):
        return str((src.parent / name).relative_to(root))
    if item.get("rename_only"):
        try:
            return str((src.parent / name).relative_to(root))
        except ValueError:
            return name
    folder = organizer.destination_dir(root, item)
    return str(((folder or root) / name).relative_to(root))


def render_plan(console: Console, plan: dict, *, limit: int = 40) -> None:
    root = Path(plan["root"])
    items = plan.get("items") or []
    table = Table(
        title=f"{len(items)} proposed change{'s' if len(items) != 1 else ''}",
        box=box.ROUNDED,
        border_style=BLUE,
        header_style=f"bold {BLUE}",
        show_lines=False,
    )
    table.add_column("#", style="dim", justify="right", width=3)
    table.add_column("✓", justify="center", width=1)
    table.add_column("Action", width=7)
    table.add_column("Before", overflow="fold")
    table.add_column("After", overflow="fold")
    for index, item in enumerate(items[:limit], start=1):
        src = Path(str(item["path"]))
        try:
            before = str(src.relative_to(root))
        except ValueError:
            before = str(src)
        selected = item.get("selected", True)
        table.add_row(
            str(index), "●" if selected else "○", _action(item),
            escape(before), escape(destination(item, root)),
            style=None if selected else "dim",
        )
    console.print(table)
    if len(items) > limit:
        console.print(f"[dim]… and {len(items) - limit} more. Save the plan to inspect every item.[/]")


def edit_item(console: Console, plan: dict, index: int, prompt) -> None:
    items = plan.get("items") or []
    if not 1 <= index <= len(items):
        console.print("[red]No item with that number.[/]")
        return
    item = items[index - 1]
    if item.get("trash"):
        console.print("[yellow]Trash destinations cannot be edited; toggle the item off instead.[/]")
        return
    root = Path(plan["root"])
    current = destination(item, root)
    value = prompt("New relative destination: ", default=current).strip()
    candidate = Path(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts:
        console.print("[red]Use a relative path inside the selected folder.[/]")
        return
    item["suggested_filename"] = candidate.name
    if item.get("folder_rename") or item.get("rename_only"):
        if candidate.parent not in (Path("."), Path("")):
            console.print("[yellow]A rename stays in its current folder; only the filename was used.[/]")
    else:
        item["dest_folder"] = None if candidate.parent == Path(".") else str(candidate.parent)
        item["category"] = "Other"
        item["subcategory"] = None


def review_plan(console: Console, plan: dict, prompt, *, default_save: Path | None = None) -> bool:
    """Interactive plan review. True means the caller may apply it."""
    while True:
        render_plan(console, plan)
        items = plan.get("items") or []
        selected = sum(item.get("selected", True) for item in items)
        if len(items) == 1:
            choices = "[y] yes · [n] no · [t] toggle · [e] edit · [s] save"
            question = "Apply this change"
        else:
            choices = "[y] yes · [n] no · [t N] toggle · [e N] edit · [s] save"
            question = f"Apply {selected} selected changes"
        answer = prompt(f"{question}? {choices}: ").strip()
        lower = answer.lower()
        if lower in {"y", "yes"}:
            return True
        if lower in {"", "n", "no", "q", "quit"}:
            return False
        if lower == "t" and len(items) == 1:
            items[0]["selected"] = not items[0].get("selected", True)
            continue
        if lower.startswith("t "):
            for token in lower[2:].replace(",", " ").split():
                if token.isdigit() and 1 <= int(token) <= len(plan["items"]):
                    item = plan["items"][int(token) - 1]
                    item["selected"] = not item.get("selected", True)
            continue
        if lower == "e" and len(items) == 1:
            edit_item(console, plan, 1, prompt)
            continue
        if lower.startswith("e ") and lower[2:].strip().isdigit():
            edit_item(console, plan, int(lower[2:].strip()), prompt)
            continue
        if lower in {"s", "save"}:
            suggested = str(default_save or Path.cwd() / "sift-plan.json")
            target = Path(prompt("Save plan to: ", default=suggested))
            console.print(f"[green]Saved[/] {save_plan(plan, target)}")
            continue
        hint = "y, n, t, e, or s" if len(items) == 1 else "y, n, t <numbers>, e <number>, or s"
        console.print(f"[yellow]Choose {hint}.[/]")


def render_answer(console: Console, result: dict) -> None:
    console.print()
    console.print(result.get("answer") or "No answer.")
    results = result.get("results") or []
    for index, item in enumerate(results, start=1):
        url = finder_url(item["path"])
        title = Text(f"\n  [{index}] {item['filename']}", style=f"bold {BLUE} link {url}")
        console.print(title)
        console.print(Text(f"  {item['path']}", style=f"dim underline link {url}"))
        if item.get("snippet"):
            snippet = clean_snippet(item["snippet"])
            if snippet:
                console.print(Text("  " + snippet.replace("\n", "\n  ")))
    if results:
        console.print("\n  [dim]/reveal selects the top result in Finder[/]")
    console.print()


def print_json(console: Console, value: object) -> None:
    console.print_json(json.dumps(value, ensure_ascii=False))
